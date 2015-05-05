# This file is part of COFFEE
#
# COFFEE is Copyright (c) 2014, Imperial College London.
# Please see the AUTHORS file in the main source directory for
# a full list of copyright holders.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * The name of Imperial College London or that of other
#       contributors may not be used to endorse or promote products
#       derived from this software without specific prior written
#       permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTERS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

from collections import defaultdict, Counter
from copy import deepcopy as dcopy
import operator
import itertools

from base import *
from utils import *
from coffee.visitors import SymbolReferences


class ExpressionRewriter(object):
    """Provide operations to re-write an expression:

    * Loop-invariant code motion: find and hoist sub-expressions which are
      invariant with respect to a loop
    * Expansion: transform an expression ``(a + b)*c`` into ``(a*c + b*c)``
    * Factorization: transform an expression ``a*b + a*c`` into ``a*(b+c)``"""

    def __init__(self, stmt, expr_info, decls, header, hoisted, expr_graph):
        """Initialize the ExpressionRewriter.

        :param stmt: AST statement containing the expression to be rewritten
        :param expr_info: ``MetaExpr`` object describing the expression in ``stmt``
        :param decls: declarations for the various symbols in ``stmt``.
        :param header: the parent Block of the loop in which ``stmt`` was found.
        :param hoisted: dictionary that tracks hoisted expressions
        :param expr_graph: expression graph that tracks symbol dependencies
        """
        self.stmt = stmt
        self.expr_info = expr_info
        self.decls = decls
        self.header = header
        self.hoisted = hoisted
        self.expr_graph = expr_graph

        # Expression manipulators used by the Expression Rewriter
        self.expr_hoister = ExpressionHoister(self.stmt,
                                              self.expr_info,
                                              self.header,
                                              self.decls,
                                              self.hoisted,
                                              self.expr_graph)
        self.expr_expander = ExpressionExpander(self.stmt,
                                                self.expr_info,
                                                self.hoisted,
                                                self.expr_graph)
        self.expr_factorizer = ExpressionFactorizer(self.stmt,
                                                    self.expr_info)

    def licm(self, **kwargs):
        """Perform generalized loop-invariant code motion.

        Loop-invariant expressions found in the nest are moved "after" the
        outermost independent loop and "after" the fastest varying dimension
        loop. Here, "after" means that if the loop nest has two loops ``i``
        and ``j``, and ``j`` is in the body of ``i``, then ``i`` comes after
        ``j`` (i.e. the loop nest has to be read from right to left).

        For example, if a sub-expression ``E`` depends on ``[i, j]`` and the
        loop nest has three loops ``[i, j, k]``, then ``E`` is hoisted out from
        the body of ``k`` to the body of ``i``). All hoisted expressions are
        then wrapped and evaluated in a new loop in order to promote compiler
        autovectorization.

        :param kwargs:
            * nrank_tmps: True if ``n``-dimensional arrays are allowed for hoisting
                expressions crossing ``n`` loops in the nest.
            * outer_only: True if only outer-loop invariant terms should be hoisted
        """
        nrank_tmps = kwargs.get('nrank_tmps')
        outer_only = kwargs.get('outer_only')

        self.expr_hoister.licm(nrank_tmps, outer_only)

    def expand(self, mode='standard'):
        """Expand expressions over other expressions based on different heuristics.
        In the simplest example one can have: ::

            (X[i] + Y[j])*F + ...

        which could be transformed into: ::

            (X[i]*F + Y[j]*F) + ...

        When creating the expanded object, if the expanding term had already been
        hoisted, then the expansion itself is also lifted. For example, if: ::

            Y[j] = f(...)
            (X[i]*Y[j])*F + ...

        and we assume it has been decided (see below) the expansion should occur
        along the loop dimension ``j``, the transformation generates: ::

            Y[j] = f(...)*F
            (X[i]*Y[j]) + ...

        One may want to expand expressions for several reasons, which include

        * Exposing factorization opportunities;
        * Exposing high-level (linear algebra) operations (e.g., matrix multiplies)
        * Relieving register pressure; when, for example, ``(X[i]*Y[j])`` is
          computed in a loop L' different than the loop L'' in which ``Y[j]``
          is evaluated, and ``cost(L') > cost(L'')``;

        :param mode: multiple expansion strategies are possible, each exposing
                     different, "hidden" opportunities for later code motion.

                     * mode == 'standard': this heuristics consists of expanding \
                                           along the loop dimension appearing \
                                           the most in different (i.e., unique) \
                                           arrays. This has the goal of making \
                                           factorization more effective.
                     * mode == 'full': expansion is performed aggressively without \
                                       any specific restrictions.
        """
        info = visit(self.stmt.children[1], search=Symbol)
        symbols = info['search'][Symbol]

        # Select the expansion strategy
        if mode == 'standard':
            # Get the ranks...
            occurrences = [s.rank for s in symbols if s.rank]
            # ...filter out irrelevant dimensions...
            occurrences = [tuple(r for r in rank if r in self.expr_info.domain)
                           for rank in occurrences]
            # ...and finally establish the expansion dimension
            dimension = Counter(occurrences).most_common(1)[0][0]
            should_expand = lambda n: set(dimension).issubset(set(n.rank))
        elif mode == 'full':
            should_expand = lambda n: \
                n.symbol in self.decls and self.decls[n.symbol].is_static_const
        else:
            warning('Unknown expansion strategy. Skipping.')
            return

        # Perform the expansion
        self.expr_expander.expand(should_expand)

        # Update known declarations
        self.decls.update(self.expr_expander.expanded_decls)

    def factorize(self, mode='standard'):
        """Factorize terms in the expression. For example: ::

            A[i]*B[j] + A[i]*C[j]

        becomes ::

            A[i]*(B[j] + C[j]).

        :param mode: multiple factorization strategies are possible, each exposing
                     different, "hidden" opportunities for code motion.

                     * mode == 'standard': this simple heuristics consists of \
                                           grouping on symbols that appear the \
                                           most in the expression.
                     * mode == 'immutable': if many static constant objects are \
                                            expected, with this strategy they are \
                                            grouped together, within the obvious \
                                            limits imposed by the expression itself.
        """
        info = visit(self.stmt.children[1], search=Symbol)
        symbols = info['search'][Symbol]

        # Select the expansion strategy
        if mode == 'standard':
            # Get the ranks...
            occurrences = [s.rank for s in symbols if s.rank]
            # ...filter out irrelevant dimensions...
            occurrences = [tuple(r for r in rank if r in self.expr_info.domain)
                           for rank in occurrences]
            # ...and finally establish the expansion dimension
            dimension = Counter(occurrences).most_common(1)[0][0]
            should_factorize = lambda n: set(dimension).issubset(set(n.rank))
        elif mode == 'immutable':
            should_factorize = lambda n: \
                n.symbol in self.decls and self.decls[n.symbol].is_static_const
        if mode not in ['standard', 'immutable']:
            warning('Unknown factorization strategy. Skipping.')
            return

        # Perform the factorization
        self.expr_factorizer.factorize(should_factorize)

    def reassociate(self):
        """Reorder symbols in associative operations following a convention.
        By default, the convention is to order the symbols based on their rank.
        For example, the terms in the expression ::

            a*b[i]*c[i][j]*d

        are reordered as ::

            a*d*b[i]*c[i][j]

        This as achieved by reorganizing the AST of the expression.
        """

        def _reassociate(node, parent):
            if isinstance(node, (Symbol, Div)):
                return

            elif isinstance(node, Par):
                _reassociate(node.child, node)

            elif isinstance(node, (Sum, Sub, FunCall)):
                for n in node.children:
                    _reassociate(n, node)

            elif isinstance(node, Prod):
                children = explore_operator(node)
                # Reassociate symbols
                symbols = [(n.rank, n, p) for n, p in children if isinstance(n, Symbol)]
                symbols.sort(key=lambda n: (len(n[0]), n[0]))
                # Capture the other children and recur on them
                other_nodes = [(n, p) for n, p in children if not isinstance(n, Symbol)]
                for n, p in other_nodes:
                    _reassociate(n, p)
                # Create the reassociated product and modify the original AST
                children = zip(*other_nodes)[0] if other_nodes else ()
                children += zip(*symbols)[1] if symbols else ()
                reassociated_node = ast_make_expr(Prod, children)
                parent.children[parent.children.index(node)] = reassociated_node

            else:
                warning('Unexpect node of type %s while reassociating', typ(node))

        _reassociate(self.stmt.children[1], self.stmt)

    @staticmethod
    def reset():
        ExpressionHoister._expr_handled[:] = []
        ExpressionExpander._expr_handled[:] = []


class ExpressionHoister(object):

    # Track all expressions to which LICM has been applied
    _expr_handled = []
    # Temporary variables template
    _hoisted_sym = "%(loop_dep)s_%(expr_id)d_%(round)d_%(i)d"

    # Constants used by the extract method to charaterize expressions:
    INVARIANT = 0  # expression is loop invariant
    SEARCH = 1  # expression is potentially within a larger invariant
    HOISTED = 2  # expression should not be hoisted any further

    def __init__(self, stmt, expr_info, header, decls, hoisted, expr_graph):
        """Initialize the ExpressionHoister."""
        self.stmt = stmt
        self.expr_info = expr_info
        self.header = header
        self.decls = decls
        self.hoisted = hoisted
        self.expr_graph = expr_graph

        # Set counters to create meaningful and unique (temporary) variable names
        try:
            self.expr_id = self._expr_handled.index(stmt)
        except ValueError:
            self._expr_handled.append(stmt)
            self.expr_id = self._expr_handled.index(stmt)
        self.counter = 0

    def _extract_exprs(self, node, dep_subexprs):
        """Extract invariant sub-expressions from the original expression.
        Hoistable sub-expressions are stored in ``dep_subexprs``."""

        def hoist(node, dep):
            should_extract = True
            if isinstance(node, Symbol):
                should_extract = False
            elif self.outer_only and dep:
                if self.expr_info.dims and dep != (self.expr_info.dims[0],):
                    should_extract = False
            if should_extract:
                dep_subexprs[dep].append(node)
            self.extracted = self.extracted or should_extract

        if isinstance(node, Symbol):
            return (self.symbols[node], self.INVARIANT)
        if isinstance(node, FunCall):
            arg_deps = [self._extract_exprs(n, dep_subexprs) for n in node.children]
            dep = tuple(set(flatten([dep for dep, _ in arg_deps])))
            info = self.INVARIANT if all([i == self.INVARIANT for _, i in arg_deps]) \
                else self.HOISTED
            return (dep, info)
        if isinstance(node, Par):
            return (self._extract_exprs(node.child, dep_subexprs))

        # Traverse the expression tree
        left, right = node.children
        dep_l, info_l = self._extract_exprs(left, dep_subexprs)
        dep_r, info_r = self._extract_exprs(right, dep_subexprs)

        # Filter out false dependencies
        dep_l = tuple(d for d in dep_l if d in self.expr_info.dims)
        dep_r = tuple(d for d in dep_r if d in self.expr_info.dims)
        dep_n = dep_l + tuple(d for d in dep_r if d not in dep_l)

        if info_l == self.SEARCH and info_r == self.SEARCH:
            if dep_l != dep_r:
                # E.g. (A[i]*alpha + D[i])*(B[j]*beta + C[j])
                hoist(left, dep_l)
                hoist(right, dep_r)
                return ((), self.HOISTED)
            else:
                # E.g. (A[i]*alpha)+(B[i]*beta)
                return (dep_l, self.SEARCH)
        elif info_l == self.SEARCH and info_r == self.INVARIANT or \
                info_l == self.INVARIANT and info_r == self.SEARCH:
            # E.g. (A[i] + B[i])*C[j], A[i]*(B[j] + C[j])
            hoist(left, dep_l)
            hoist(right, dep_r)
            return ((), self.HOISTED)
        elif info_l == self.INVARIANT and info_r == self.INVARIANT:
            if dep_l == dep_r:
                # E.g. alpha*beta, A[i] + B[i]
                return (dep_l, self.INVARIANT)
            elif dep_l and not dep_r:
                # E.g. A[i]*alpha
                hoist(right, dep_r)
                return (dep_l, self.SEARCH)
            elif dep_r and not dep_l:
                # E.g. alpha*A[i]
                hoist(left, dep_l)
                return (dep_r, self.SEARCH)
            else:
                # must be: dep_l and dep_r but dep_l != dep_r
                if set(dep_l).issubset(set(dep_r)):
                    # E.g. A[i]*B[i,j]
                    return (dep_r, self.SEARCH)
                elif set(dep_r).issubset(set(dep_l)):
                    # E.g. A[i,j]*B[i]
                    return (dep_l, self.SEARCH)
                else:
                    # E.g. A[i]*B[j]
                    if self.nrank_tmps:
                        hoist(node, dep_n)
                    else:
                        hoist(left, dep_l)
                        hoist(right, dep_r)
                    return ((), self.HOISTED)
        else:
            # must be: info_l == self.HOISTED or info_r == self.HOISTED
            if info_r in [self.INVARIANT, self.SEARCH]:
                hoist(right, dep_r)
            elif info_l in [self.INVARIANT, self.SEARCH]:
                hoist(left, dep_l)
            return ((), self.HOISTED)

    def _check_loops(self, loops):
        """Ensures hoisting is legal. As long as all inner loops are perfect,
        hoisting at the bottom of the possibly non-perfect outermost loop
        always is a legal transformation."""
        return all([is_perfect_loop(l) for l in loops[1:]])

    def licm(self, nrank_tmps, outer_only):
        """Perform generalized loop-invariant code motion."""
        if not self._check_loops(self.expr_info.loops):
            warning("Loop nest unsuitable for generalized licm. Skipping.")
            return

        # (Re)set global parameters for the /extract/ function
        self.symbols = visit(self.header, None)['symbols_dep']
        self.symbols = dict((s, [l.dim for l in dep]) for s, dep in self.symbols.items())
        self.extracted = False
        self.nrank_tmps = nrank_tmps
        self.outer_only = outer_only

        # Extract read-only sub-expressions that do not depend on at least
        # one loop in the loop nest
        expr_dims_loops = self.expr_info.loops_from_dims
        inv_dep = {}
        while True:
            dep_subexprs = defaultdict(list)
            self._extract_exprs(self.stmt.children[1], dep_subexprs)

            # While end condition
            if not self.extracted:
                break

            self.extracted = False
            self.counter += 1

            for dep, subexprs in dep_subexprs.items():
                # -1) Filter dependencies that do not pertain to the expression
                # and remove identical subexpressions
                subexprs = dict([(str(e), e) for e in subexprs]).values()

                # 0) Determine the loop nest level where invariant expressions
                # should be hoisted. The goal is to hoist them as far as possible
                # in the loop nest, while minimising temporary storage.
                # We distinguish five hoisting cases:
                if len(dep) == 0:
                    # As scalar (/wrap_loop=None/), outside of the loop nest;
                    place = self.header
                    wrap_loop = ()
                    next_loop = self.expr_info.out_loop
                elif len(dep) == 1 and is_perfect_loop(self.expr_info.out_loop):
                    # As vector, outside of the loop nest;
                    place = self.header
                    wrap_loop = (expr_dims_loops[dep[0]],)
                    next_loop = self.expr_info.out_loop
                elif len(dep) == 1 and len(expr_dims_loops) > 1:
                    # As scalar, within the loop imposing the dependency
                    place = expr_dims_loops[dep[0]].children[0]
                    wrap_loop = ()
                    next_loop = od_find_next(expr_dims_loops, dep[0])
                elif len(dep) == 1:
                    # As scalar, at the bottom of the loop imposing the dependency
                    place = expr_dims_loops[dep[0]].children[0]
                    wrap_loop = ()
                    next_loop = place.children[-1]
                else:
                    # As vector, within the outermost loop imposing the dependency
                    dep_block = expr_dims_loops[dep[0]].children[0]
                    place = dep_block
                    wrap_loop = tuple(expr_dims_loops[dep[i]] for i in range(1, len(dep)))
                    next_loop = od_find_next(expr_dims_loops, dep[0])

                # 1) Create the new invariant sub-expressions and temporaries
                loop_size = tuple([l.size for l in wrap_loop])
                loop_dim = tuple([l.dim for l in wrap_loop])
                hoisted_syms = [Symbol(self._hoisted_sym % {
                    'loop_dep': '_'.join(dep).upper() if dep else 'CONST',
                    'expr_id': self.expr_id,
                    'round': self.counter,
                    'i': i
                }, loop_size) for i in range(len(subexprs))]
                hoisted_decls = [Decl(self.expr_info.type, s) for s in hoisted_syms]
                inv_loop_syms = [Symbol(d.sym.symbol, loop_dim) for d in hoisted_decls]

                # 2) Create the new for loop containing invariant statements
                _subexprs = [Par(dcopy(e)) if not isinstance(e, Par) else dcopy(e)
                             for e in subexprs]
                inv_loop = [Assign(s, e) for s, e in zip(dcopy(inv_loop_syms), _subexprs)]

                # 3) Update the dictionary of known declarations
                for d in hoisted_decls:
                    d.scope = LOCAL
                    self.decls[d.sym.symbol] = d

                # 4) Replace invariant sub-trees with the proper tmp variable
                to_replace = dict(zip(subexprs, inv_loop_syms))
                n_replaced = ast_replace(self.stmt.children[1], to_replace)

                # 5) Track hoisted symbols and symbols dependencies
                sym_info = [(i, j, inv_loop) for i, j in zip(_subexprs, hoisted_decls)]
                self.hoisted.update(zip([s.symbol for s in inv_loop_syms], sym_info))
                for s, e in zip(inv_loop_syms, subexprs):
                    self.expr_graph.add_dependency(s, e, n_replaced[str(s)] > 1)
                    self.symbols[s] = dep

                # 6a) Update expressions hoisted along a known dimension (same dep)
                inv_info = (loop_dim, place, next_loop, wrap_loop)
                if inv_info in inv_dep:
                    inv_dep[inv_info][0].extend(hoisted_decls)
                    inv_dep[inv_info][1].extend(inv_loop)
                    continue

                # 6b) Keep track of hoisted stuff
                inv_dep[inv_info] = (hoisted_decls, inv_loop)

        for inv_info, (hoisted_decls, inv_loop) in sorted(inv_dep.items()):
            loop_dim, place, next_loop, wrap_loop = inv_info
            # Create the hoisted code
            if wrap_loop:
                wrap_loop = dcopy(wrap_loop)
                wrap_loop[-1].body[:] = inv_loop
                inv_loop = inv_code = [wrap_loop[0]]
            else:
                inv_code = [None]
            # Insert the new nodes at the right level in the loop nest
            ofs = place.children.index(next_loop)
            place.children[ofs:ofs] = hoisted_decls + inv_loop + [FlatBlock("\n")]
            # Update hoisted symbols metadata
            for i in hoisted_decls:
                self.hoisted.update_stmt(i.sym.symbol, loop=inv_code[0], place=place)


class ExpressionExpander(object):

    GRP = 0
    EXP = 1

    # Track all expanded expressions
    _expr_handled = []
    # Temporary variables template
    _expanded_sym = "%(loop_dep)s_EXP_%(expr_id)d_%(i)d"

    def __init__(self, stmt, expr_info, hoisted, expr_graph):
        self.stmt = stmt
        self.expr_info = expr_info
        self.hoisted = hoisted
        self.expr_graph = expr_graph

        # Set counters to create meaningful and unique (temporary) variable names
        try:
            self.expr_id = self._expr_handled.index(stmt)
        except ValueError:
            self._expr_handled.append(stmt)
            self.expr_id = self._expr_handled.index(stmt)

        self.expanded_decls = {}
        self.cache = {}

    def _hoist(self, expansion, exp, grp):
        """Expand an hoisted expression. If there are no data dependencies,
        the hoisted expression is expanded and no new symbols are introduced.
        Otherwise, (e.g., the symbol to be expanded appears multiple times, or
        it depends on other hoisted symbols), create a new symbol."""
        # First, check if any of the symbols in /exp/ have been hoisted
        try:
            exp = [s for s in visit(exp)['symbols_dep'].keys()
                   if s.symbol in self.hoisted and self.should_expand(s)][0]
        except:
            # No hoisted symbols in the expanded expression, so return
            return {}

        # Aliases
        hoisted_expr = self.hoisted[exp.symbol].expr
        hoisted_decl = self.hoisted[exp.symbol].decl
        hoisted_loop = self.hoisted[exp.symbol].loop
        hoisted_place = self.hoisted[exp.symbol].place
        op = expansion.__class__

        # Is the grouped symbol hoistable, or does it break some data dependency?
        grp_symbols = SymbolReferences().visit(grp).keys()
        for l in reversed(self.expr_info.loops):
            for g in grp_symbols:
                g_refs = self.info['symbol_refs'][g]
                g_deps = set(flatten([self.info['symbols_dep'][r[0]] for r in g_refs]))
                if any([l.dim in g.dim for g in g_deps]):
                    return {}
            if l in hoisted_place.children:
                break

        # The expression used for expansion is assigned to a temporary value in order
        # to minimize code size
        if str(grp) in self.cache:
            grp = dcopy(self.cache[str(grp)])
        elif not isinstance(grp, Symbol):
            grp_sym = Symbol(self._expanded_sym % {'loop_dep': 'CONST',
                                                   'expr_id': self.expr_id,
                                                   'i': len(self.cache)})
            grp_decl = Decl(self.expr_info.type, dcopy(grp_sym), grp)
            grp_decl.scope = LOCAL
            # Track the new temporary
            self.expanded_decls[grp_decl.sym.symbol] = grp_decl
            self.cache[str(grp)] = grp_sym
            self.expr_graph.add_dependency(grp_sym, grp, False)
            # Update the AST
            insert_at_elem(hoisted_place.children, hoisted_loop, grp_decl)
            grp = grp_sym

        # No dependencies, just perform the expansion
        if not self.expr_graph.has_dep(exp):
            hoisted_expr.children[0] = op(Par(hoisted_expr.children[0]), dcopy(grp))
            self.expr_graph.add_dependency(exp, grp, False)
            return {exp: exp}

        # Create new symbol, expression, and declaration
        expr = Par(op(dcopy(exp), grp))
        hoisted_exp = dcopy(exp)
        hoisted_exp.symbol = self._expanded_sym % {'loop_dep': exp.symbol,
                                                   'expr_id': self.expr_id,
                                                   'i': len(self.expanded_decls)}
        decl = dcopy(hoisted_decl)
        decl.sym.symbol = hoisted_exp.symbol
        # Update the AST
        hoisted_loop.body.append(Assign(hoisted_exp, expr))
        insert_at_elem(hoisted_place.children, hoisted_decl, decl)
        # Update tracked information
        self.expanded_decls[decl.sym.symbol] = decl
        self.hoisted[hoisted_exp.symbol] = (expr, decl, hoisted_loop, hoisted_place)
        self.expr_graph.add_dependency(hoisted_exp, expr, 0)
        return {exp: hoisted_exp}

    def _expand(self, node, parent):
        if isinstance(node, Symbol):
            return ([node], self.EXP) if self.should_expand(node) else ([node], self.GRP)

        elif isinstance(node, Par):
            return self._expand(node.child, node)

        elif isinstance(node, (Div, FunCall)):
            return ([node], self.GRP)

        elif isinstance(node, Prod):
            l_exps, l_type = self._expand(node.left, node)
            r_exps, r_type = self._expand(node.right, node)
            if l_type == self.GRP and r_type == self.GRP:
                return ([node], self.GRP)
            # At least one child is expandable (marked as EXP), whereas the
            # other could either be expandable as well or groupable (marked
            # as GRP): so we can perform the expansion
            groupable, expandable, expanding_child = r_exps, l_exps, node.left
            if l_type == self.GRP:
                groupable, expandable, expanding_child = l_exps, r_exps, node.right
            to_replace = defaultdict(list)
            for exp, grp in itertools.product(expandable, groupable):
                expansion = node.__class__(exp, dcopy(grp))
                if exp == expanding_child:
                    # Implies /expandable/ contains just one node, /e/
                    expanding_child = expansion
                    break
                to_replace[exp].append(expansion)
                self.expansions.append(expansion)
            ast_replace(node, {k: ast_make_expr(Sum, v) for k, v in to_replace.items()},
                        mode='symbol')
            # Update the parent node, since an expression has just been expanded
            parent.children[parent.children.index(node)] = expanding_child
            return (list(flatten(to_replace.values())) or [expanding_child], self.EXP)

        elif isinstance(node, (Sum, Sub)):
            l_exps, l_type = self._expand(node.left, node)
            r_exps, r_type = self._expand(node.right, node)
            if l_type == self.EXP and r_type == self.EXP and isinstance(node, Sum):
                return (l_exps + r_exps, self.EXP)
            elif l_type == self.EXP and r_type == self.EXP and isinstance(node, Sub):
                return (l_exps + [Neg(r) for r in r_exps], self.EXP)
            else:
                return ([node], self.GRP)

        else:
            raise RuntimeError("Expansion error: unknown node: %s" % str(node))

    def expand(self, should_expand):
        """Perform the expansion of the expression rooted in ``self.stmt``.
        Symbols for which the lambda function ``should_expand`` returns
        True are expansion candidates."""
        # Preload and set data structures for expansion
        self.expansions = []
        self.should_expand = should_expand
        self.info = visit(self.expr_info.out_loop)

        # Expand according to the /should_expand/ heuristic
        self._expand(self.stmt.children[1], self.stmt)

        # Now see if some of the expanded terms are groupable at the level
        # of hoisted expressions
        to_replace, to_remove = {}, set()
        for expansion in self.expansions:
            exp, grp = expansion.left, expansion.right
            hoisted = self._hoist(expansion, exp, grp)
            if hoisted:
                to_replace.update(hoisted)
                to_remove.add(grp)
        ast_replace(self.stmt, to_replace, copy=True)
        ast_remove(self.stmt, to_remove)


class ExpressionFactorizer(object):

    class Term():

        def __init__(self, operands, factors=None, op=None):
            # Example: in the Term /a*(b+c)/, /a/ is an 'operand', /b/ and /c/
            # are 'factors', and /+/ is the 'op'
            self.operands = operands
            self.factors = factors or set()
            self.op = op

        @property
        def operands_ast(self):
            # Exploiting associativity, establish an order for the operands
            operands = sorted(list(self.operands), key=lambda o: str(o))
            return ast_make_expr(Prod, tuple(operands))

        @property
        def factors_ast(self):
            factors = sorted(list(self.factors), key=lambda f: str(f))
            return ast_make_expr(self.op, tuple(factors))

        @property
        def generate_ast(self):
            if len(self.factors) == 0:
                return self.operands_ast
            elif len(self.operands) == 0:
                return self.factors_ast
            else:
                return Prod(self.operands_ast, self.factors_ast)

        @staticmethod
        def process(symbols, should_factorize, op=None):
            operands = set(s for s in symbols if should_factorize(s))
            factors = set(s for s in symbols if not should_factorize(s))
            return ExpressionFactorizer.Term(operands, factors, op)

    def __init__(self, stmt, expr_info):
        self.stmt = stmt
        self.expr_info = expr_info

    def _simplify_sum(self, terms):
        unique_terms = {}
        for t in terms:
            unique_terms.setdefault(str(t.generate_ast), list()).append(t)

        for t_repr, t_list in unique_terms.items():
            occurrences = len(t_list)
            unique_terms[t_repr] = t_list[0]
            if occurrences > 1:
                unique_terms[t_repr].operands.add(Symbol(occurrences))

        terms[:] = unique_terms.values()

    def _factorize(self, node, parent):
        if isinstance(node, Symbol):
            return self.Term.process([node], self.should_factorize)

        elif isinstance(node, Par):
            return self._factorize(node.child, node)

        elif isinstance(node, (FunCall, Div)):
            return self.Term(set([node]))

        elif isinstance(node, Prod):
            children = explore_operator(node)
            symbols = [n for n, _ in children if isinstance(n, Symbol)]
            other_nodes = [(n, p) for n, p in children if n not in symbols]
            term = self.Term.process(symbols, self.should_factorize, Prod)
            for n, p in other_nodes:
                term.operands |= self._factorize(n, p).operands
            return term

        # The fundamental case is when /node/ is a Sum (or Sub, equivalently).
        # Here, we try to factorize the terms composing the operation
        elif isinstance(node, (Sum, Sub)):
            children = explore_operator(node)
            # First try to factorize within /node/'s children
            terms = [self._factorize(n, p) for n, p in children]
            # Then check if it's possible to aggregate operations
            # Example: replace (a*b)+(a*b) with 2*(a*b)
            self._simplify_sum(terms)
            # Finally try to factorize some of the operands composing the operation
            factorized = {}
            for t in terms:
                operand = set([t.operands_ast]) if t.operands else set()
                factor = set([t.factors_ast]) if t.factors else set()
                factorized_term = self.Term(operand, factor, node.__class__)
                _t = factorized.setdefault(str(t.operands_ast), factorized_term)
                _t.factors |= factor
            factorized = ast_make_expr(Sum, [t.generate_ast for t in factorized.values()])
            parent.children[parent.children.index(node)] = factorized
            return self.Term(set([factorized]))

        else:
            raise RuntimeError("Factorization error: unknown node: %s" % str(node))

    def factorize(self, should_factorize):
        self.should_factorize = should_factorize
        self._factorize(self.stmt.children[1], self.stmt)
