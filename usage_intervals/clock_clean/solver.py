"""Joint disambiguation of clock candidates without enumerating interpretations.

Each logical record is a variable with a tiny finite domain: one UTC candidate
for ordinary/anchor records, at most two for fall-back labels.  Two kinds of
binary relations constrain them across *all* input files at once:

* stream order (records of one stream sorted by ``seq``, anchors included):
  UTC instants must be non-decreasing (``<=``);
* session order (same ``(device, session_id)`` having both ends, even across
  streams): enter must be strictly earlier than exit (``<``).

Every forbidden value pair of a relation is a binary clause over the
"record takes this candidate" literals, and one-candidate domains add unit
clauses.  The whole system is therefore a 2-SAT formula.  Tarjan's SCC gives
satisfiability, the *exact* feasible candidate set of every record (a literal
is feasible exactly when it does not imply its own negation) and, on demand,
full satisfying assignments for explanation instances.  Complexity is linear
in the number of records and relations; interpretation combinations are never
enumerated, so chains of independent two-candidate sessions stay cheap.
"""

from __future__ import annotations

from dataclasses import dataclass

LE = "non_decreasing"      # stream seq order: t_i <= t_j
LT = "strictly_before"     # session enter < exit


@dataclass(frozen=True)
class Relation:
    kind: str                       # LE or LT
    left_id: str                    # record_id of the earlier/enter variable
    right_id: str                   # record_id of the later/exit variable
    detail: str                     # human-readable provenance of the relation


class UnsatisfiableClockError(ValueError):
    def __init__(self, message, records, relations):
        super().__init__(message)
        self.records = list(records)
        self.relations = list(relations)


class _SATSolver:
    def __init__(self, nvars: int):
        self.nvars = nvars
        self.nlits = 2 * nvars
        self.graph = [[] for _ in range(self.nlits)]
        self.clauses = []
        self._comp = None
        self._dag_succ = None

    @staticmethod
    def lit(var: int, cand_idx: int) -> int:
        return 2 * var + cand_idx

    def add_clause(self, a: int, b: int) -> None:
        # (a OR b)  <=>  (!a -> b) and (!b -> a)
        self.graph[a ^ 1].append(b)
        self.graph[b ^ 1].append(a)
        self.clauses.append((a, b))

    def add_unit(self, a: int) -> None:
        self.add_clause(a, a)

    def tarjan(self, graph=None):
        """Iterative Tarjan; component ids are reverse-topological (sinks first)."""
        graph = self.graph if graph is None else graph
        n = len(graph)
        index = [-1] * n
        low = [0] * n
        onstack = [False] * n
        stack = []
        comp = [-1] * n
        next_index = 0
        next_comp = 0

        for root in range(n):
            if index[root] != -1:
                continue
            index[root] = low[root] = next_index
            next_index += 1
            stack.append(root)
            onstack[root] = True
            work = [(root, 0)]
            while work:
                node, ni = work[-1]
                if ni < len(graph[node]):
                    w = graph[node][ni]
                    work[-1] = (node, ni + 1)
                    if index[w] == -1:
                        index[w] = low[w] = next_index
                        next_index += 1
                        stack.append(w)
                        onstack[w] = True
                        work.append((w, 0))
                    elif onstack[w]:
                        low[node] = min(low[node], index[w])
                else:
                    if low[node] == index[node]:
                        while True:
                            w = stack.pop()
                            onstack[w] = False
                            comp[w] = next_comp
                            if w == node:
                                break
                        next_comp += 1
                    work.pop()
                    if work:
                        parent = work[-1][0]
                        low[parent] = min(low[parent], low[node])
        return comp, next_comp

    def solve(self, assumptions=()):
        """Satisfy the clauses plus optional unit assumptions.

        Returns ``(comp, ncomp, assignment)`` with assignment[var] the chosen
        candidate index, or ``None`` when unsatisfiable.
        """
        if assumptions:
            graph = [list(adj) for adj in self.graph]
            for lit in assumptions:
                graph[lit ^ 1].append(lit)
            comp, ncomp = self.tarjan(graph)
        else:
            comp, ncomp = self.tarjan()
            self._comp = comp
            self._dag_succ = self._condensation(comp, ncomp)
        for v in range(self.nvars):
            if comp[2 * v] == comp[2 * v + 1]:
                return comp, ncomp, None
        # Tarjan numbers sink SCCs first; a literal is true when its component
        # id is smaller than its negation's.
        assignment = [
            0 if comp[2 * v] < comp[2 * v + 1] else 1
            for v in range(self.nvars)
        ]
        return comp, ncomp, assignment

    def _condensation(self, comp, ncomp):
        succ = [set() for _ in range(ncomp)]
        for u, adj in enumerate(self.graph):
            for w in adj:
                if comp[u] != comp[w]:
                    succ[comp[u]].add(comp[w])
        return succ

    def literal_feasible(self, var, cand_idx) -> bool:
        """Feasible iff the literal cannot reach its own negation."""
        lit = self.lit(var, cand_idx)
        src, dst = self._comp[lit], self._comp[lit ^ 1]
        if src == dst:
            return False
        seen = {src}
        stack = [src]
        while stack:
            c = stack.pop()
            if c == dst:
                return False
            for w in self._dag_succ[c]:
                if w not in seen:
                    seen.add(w)
                    stack.append(w)
        return True


@dataclass
class SolvedVariable:
    record_id: str
    candidates: tuple           # initial UTC candidates (one or two)
    feasible: tuple             # candidates surviving all joint constraints
    determined: bool
    value: int | None           # the single feasible UTC ms when determined


@dataclass
class SolveResult:
    variables: dict             # record_id -> SolvedVariable
    order: tuple                # record ids in variable order
    relations: tuple            # all relations with provenance
    domains: dict               # record_id -> tuple of candidate ms
    connected: dict             # record_id -> undirected relation-component id
    comp_members: dict          # component id -> list of record ids
    solver: object = None       # the _SATSolver (kept for explain instances)


def build_and_solve(records, domains):
    """Build constraints from loaded logical records and solve globally.

    ``records`` is the ordered tuple of RawRecord; ``domains`` maps record_id
    to its tuple of candidate UTC milliseconds (empty tuple already rejected
    by the caller).  Raises :class:`UnsatisfiableClockError` when no joint
    interpretation exists.
    """
    order = tuple(r.record_id for r in records)
    idx = {rid: i for i, rid in enumerate(order)}
    solver = _SATSolver(len(order))
    relations = []

    # unit clauses for singleton domains.  A variable with one candidate has
    # only literal 0 ("takes the candidate"); literal 1 is an unused encoding
    # slot, so it is forced false explicitly.  Without (!lit1) the SCC routine
    # could see the two literals of one variable in the same component and
    # wrongly declare the formula unsatisfiable.
    for rid in order:
        v = idx[rid]
        if len(domains[rid]) == 1:
            solver.add_unit(_SATSolver.lit(v, 0))
            solver.add_unit(_SATSolver.lit(v, 1) ^ 1)  # unused slot forced false

    # stream order: sort each stream by seq (never by file/line order)
    by_stream: dict[str, list] = {}
    for rec in records:
        by_stream.setdefault(rec.stream, []).append(rec)
    for stream, recs in sorted(by_stream.items()):
        recs.sort(key=lambda r: r.seq)
        for prev, nxt in zip(recs, recs[1:]):
            rel = Relation(
                LE, prev.record_id, nxt.record_id,
                f"stream {stream!r}: seq {prev.seq} <= seq {nxt.seq} (non-decreasing UTC)",
            )
            relations.append(rel)
            _emit_binary(solver, idx, domains, rel, strict=False)

    # session order: enter strictly before exit, possibly on different streams
    ends: dict[tuple, dict] = {}
    for rec in records:
        if rec.type != "door":
            continue
        ends.setdefault((rec.device, rec.session_id), {}).setdefault(rec.kind, []).append(rec)
    for (device, sid), kinds in sorted(ends.items()):
        for en in kinds.get("enter", []):
            for ex in kinds.get("exit", []):
                rel = Relation(
                    LT, en.record_id, ex.record_id,
                    f"session ({device!r}, {sid!r}): enter {en.record_id!r} "
                    f"strictly before exit {ex.record_id!r}"
                    + (f" across streams {en.stream!r}->{ex.stream!r}"
                       if en.stream != ex.stream else ""),
                )
                relations.append(rel)
                _emit_binary(solver, idx, domains, rel, strict=True)

    comp, ncomp, assignment = solver.solve()
    if assignment is None:
        bad = [order[v] for v in range(len(order))
               if comp[2 * v] == comp[2 * v + 1]]
        incident = {rid for rid in bad}
        rels = [rel for rel in relations
                if rel.left_id in incident or rel.right_id in incident]
        if not rels:  # defensive: still surface the relations touching the part
            rels = list(relations)
        raise UnsatisfiableClockError(
            "no joint interpretation satisfies the stream/session constraints; "
            f"records involved: {bad}",
            bad, rels,
        )

    variables = {}
    for rid in order:
        v = idx[rid]
        dom = domains[rid]
        feasible = tuple(dom[k] for k in range(len(dom))
                         if solver.literal_feasible(v, k))
        determined = len(feasible) == 1
        variables[rid] = SolvedVariable(
            record_id=rid, candidates=dom, feasible=feasible,
            determined=determined, value=feasible[0] if determined else None,
        )

    connected, comp_members = _relation_components(records, relations)
    return SolveResult(
        variables=variables, order=order, relations=tuple(relations),
        domains=domains, connected=connected, comp_members=comp_members,
        solver=solver,
    )


def _emit_binary(solver, idx, domains, rel: Relation, strict: bool) -> None:
    li, ri = idx[rel.left_id], idx[rel.right_id]
    ldom, rdom = domains[rel.left_id], domains[rel.right_id]
    for ai, a in enumerate(ldom):
        for bi, b in enumerate(rdom):
            violates = (a >= b) if strict else (a > b)
            if violates:
                # forbid (left=a AND right=b): (!P_left,a OR !P_right,b)
                solver.add_clause(
                    _SATSolver.lit(li, ai) ^ 1,
                    _SATSolver.lit(ri, bi) ^ 1,
                )


def _relation_components(records, relations):
    """Undirected connected components of the relation graph."""
    adj: dict[str, set] = {r.record_id: set() for r in records}
    for rel in relations:
        adj[rel.left_id].add(rel.right_id)
        adj[rel.right_id].add(rel.left_id)
    comp_of, members = {}, {}
    cid = 0
    for rid in adj:
        if rid in comp_of:
            continue
        stack = [rid]
        comp_of[rid] = cid
        members[cid] = []
        while stack:
            cur = stack.pop()
            members[cid].append(cur)
            for w in adj[cur]:
                if w not in comp_of:
                    comp_of[w] = cid
                    stack.append(w)
        cid += 1
    return comp_of, members


def explain_candidate(result: SolveResult, solver, record_id: str,
                      candidate_ms: int) -> dict | None:
    """Force one feasible candidate and return a full assignment for its part.

    The returned times satisfy *all* relations in the record's relation
    connected component simultaneously -- never a patchwork of locally valid
    choices.  Returns None when the candidate is not feasible for this record.
    """
    sv = result.variables[record_id]
    if candidate_ms not in sv.feasible:
        return None
    idx = {rid: i for i, rid in enumerate(result.order)}
    var = idx[record_id]
    # singleton domains encode their only candidate at literal 0; two-candidate
    # domains use the candidate's position in the domain tuple
    cand_idx = result.domains[record_id].index(candidate_ms)
    assumption_lit = _SATSolver.lit(var, cand_idx)
    _, _, assignment = solver.solve(assumptions=[assumption_lit])
    if assignment is None:  # pragma: no cover - feasibility was established
        return None
    cid = result.connected[record_id]
    instances = {}
    for rid2 in result.comp_members[cid]:
        v2 = idx[rid2]
        # singleton domains only have literal 0; the SCC truth value of the
        # unused literal must not index past the end of a one-element domain
        chosen_idx = min(assignment[v2], len(result.domains[rid2]) - 1)
        instances[rid2] = result.domains[rid2][chosen_idx]
    return instances
