# -*- coding: utf-8 -*-

"""Main module."""

import torch
from torch.autograd import Function
import osqp


# TODO: Finish from this
# https://github.com/sbarratt/diff_osqp/blob/master/diff_osqp.py


class OSQP(Function):
    def __init__(self,
                 eps=1e-12,
                 verbose=0,
                 notImprovedLim=3,
                 maxIter=20,
                 solver=QPSolvers.PDIPM_BATCHED,
                 check_Q_spd=True):
        self.eps = eps
        self.verbose = verbose
        self.notImprovedLim = notImprovedLim
        self.maxIter = maxIter
        self.solver = solver
        self.check_Q_spd = check_Q_spd

    def forward(self, Q_, p_, A_, l_, u_):
        """Solve a batch of QPs using OSQP.

        This function solves a batch of QPs, each optimizing over
        `n` variables and having `m` constraints.

        The optimization problem for each instance in the batch
        (dropping indexing from the notation) is of the form

            \hat z =   argmin_z 1/2 z' P z + q' z
                       subject to l <= Ax <= u
                                

        where P \in S^{n,n},
              S^{n,n} is the set of all positive semi-definite matrices,
              q \in R^{n}
              A \in R^{m,n}
              l \in R^{m}
              u \in R^{m}
              
        These parameters should all be passed to this function as
        Variable- or Parameter-wrapped Tensors.
        (See torch.autograd.Variable and torch.nn.parameter.Parameter)

        If you want to solve a batch of QPs where `n` and `m`
        are the same, but some of the contents differ across the
        minibatch, you can pass in tensors in the standard way
        where the first dimension indicates the batch example.
        This can be done with some or all of the coefficients.

        You do not need to add an extra dimension to coefficients
        that will not change across all of the minibatch examples.
        This function is able to infer such cases.

        If you don't want to use any constraints, you can set the appropriate values to:

            e = Variable(torch.Tensor())

        Parameters:
          P:  A (n_batch, n, n) or (n, n) Tensor.
          q:  A (n_batch, n) or (n) Tensor.
          A:  A (n_batch, m, n) or (m, n) Tensor.
          l:  A (n_batch, m) or (m) Tensor.
          u:  A (n_batch, m) or (m) Tensor.
          
        Returns: \hat z: a (n_batch, n) Tensor.
        """
        n_batch = extract_nBatch(Q_, p_, G_, h_, A_, b_)
        Q, _ = expandParam(Q_, nBatch, 3)
        p, _ = expandParam(p_, nBatch, 2)
        G, _ = expandParam(G_, nBatch, 3)
        h, _ = expandParam(h_, nBatch, 2)
        A, _ = expandParam(A_, nBatch, 3)
        b, _ = expandParam(b_, nBatch, 2)

        # (Bart): This is expensive!
        # if self.check_P_spd:
        #     for i in range(n_batch):
        #         e, _ = torch.eig(Q[i])
        #         if not torch.all(e[:,0] > 0):
        #             raise RuntimeError('Q is not SPD.')

        _, nineq, nz = G.size()
        neq = A.size(1) if A.nelement() > 0 else 0
        assert(neq > 0 or nineq > 0)
        self.neq, self.nineq, self.nz = neq, nineq, nz

        # if self.solver == QPSolvers.PDIPM_BATCHED:
        #     self.Q_LU, self.S_LU, self.R = pdipm_b.pre_factor_kkt(Q, G, A)
        #     zhats, self.nus, self.lams, self.slacks = pdipm_b.forward(
        #         Q, p, G, h, A, b, self.Q_LU, self.S_LU, self.R,
        #         self.eps, self.verbose, self.notImprovedLim, self.maxIter)
        # elif self.solver == QPSolvers.CVXPY:
        vals = torch.Tensor(nBatch).type_as(Q)
        zhats = torch.Tensor(nBatch, self.nz).type_as(Q)
        lams = torch.Tensor(nBatch, self.nineq).type_as(Q)
        nus = torch.Tensor(nBatch, self.neq).type_as(Q) \
              if self.neq > 0 else torch.Tensor()
        slacks = torch.Tensor(nBatch, self.nineq).type_as(Q)

        # TODO: Write for loop and solve with OSQP
        # TODO: Can make it faster if only vectors change!
        for i in range(nBatch):
             Ai, bi = (A[i], b[i]) if neq > 0 else (None, None)
             vals[i], zhati, nui, lami, si = solvers.cvxpy.forward_single_np(
                 *[x.cpu().numpy() if x is not None else None
                   for x in (Q[i], p[i], G[i], h[i], Ai, bi)])
             # if zhati[0] is None:
             #     import IPython, sys; IPython.embed(); sys.exit(-1)
             zhats[i] = torch.Tensor(zhati)
             lams[i] = torch.Tensor(lami)
             slacks[i] = torch.Tensor(si)
             if neq > 0:
                 nus[i] = torch.Tensor(nui)
             self.vals = vals
             self.lams = lams
             self.nus = nus
             self.slacks = slacks
        # else:
        #     assert False

        self.save_for_backward(zhats, Q_, p_, G_, h_, A_, b_)
        return zhats

    def backward(self, dl_dzhat):
        zhats, Q, p, G, h, A, b = self.saved_tensors
        nBatch = extract_nBatch(Q, p, G, h, A, b)
        Q, Q_e = expandParam(Q, nBatch, 3)
        p, p_e = expandParam(p, nBatch, 2)
        G, G_e = expandParam(G, nBatch, 3)
        h, h_e = expandParam(h, nBatch, 2)
        A, A_e = expandParam(A, nBatch, 3)
        b, b_e = expandParam(b, nBatch, 2)

        # neq, nineq, nz = self.neq, self.nineq, self.nz
        neq, nineq = self.neq, self.nineq


        if self.solver == QPSolvers.CVXPY:
            self.Q_LU, self.S_LU, self.R = pdipm_b.pre_factor_kkt(Q, G, A)

        # Clamp here to avoid issues coming up when the slacks are too small.
        # TODO: A better fix would be to get lams and slacks from the
        # solver that don't have this issue.
        d = torch.clamp(self.lams, min=1e-8) / torch.clamp(self.slacks, min=1e-8)

        pdipm_b.factor_kkt(self.S_LU, self.R, d)
        dx, _, dlam, dnu = pdipm_b.solve_kkt(
            self.Q_LU, d, G, A, self.S_LU,
            dl_dzhat, torch.zeros(nBatch, nineq).type_as(G),
            torch.zeros(nBatch, nineq).type_as(G),
            torch.zeros(nBatch, neq).type_as(G) if neq > 0 else torch.Tensor())

        dps = dx
        dGs = bger(dlam, zhats) + bger(self.lams, dx)
        if G_e:
            dGs = dGs.mean(0)
        dhs = -dlam
        if h_e:
            dhs = dhs.mean(0)
        if neq > 0:
            dAs = bger(dnu, zhats) + bger(self.nus, dx)
            dbs = -dnu
            if A_e:
                dAs = dAs.mean(0)
            if b_e:
                dbs = dbs.mean(0)
        else:
            dAs, dbs = None, None
        dQs = 0.5 * (bger(dx, zhats) + bger(zhats, dx))
        if Q_e:
            dQs = dQs.mean(0)
        if p_e:
            dps = dps.mean(0)


        grads = (dQs, dps, dGs, dhs, dAs, dbs)

        return grads
