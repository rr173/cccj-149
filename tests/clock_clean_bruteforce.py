"""Independent exhaustive cross-checker for the clock-cleaning solver.

For *small* inputs this module enumerates every candidate combination
(product of per-record domains) and derives, from scratch:

* whether the whole import is satisfiable;
* the exact feasible candidate set of every record;
* export / quarantine decisions at the original-session level;
* a full joint satisfying instance for every feasible candidate of a record.

It deliberately imports ONLY the structural loader and the timezone
primitives -- never ``clock_clean.solver`` / ``pipeline`` -- so an agreement
between the two is an independent check rather than shared code agreeing with
itself.
"""

from __future__ import annotations

import itertools

from usage_intervals.clock_clean.raw import ANCHOR, DOOR, load_manifest
from usage_intervals.clock_clean.timezones import local_candidates, utc_ms_candidate


class BruteForce:
    def __init__(self, manifest_path):
        self.loaded = load_manifest(manifest_path)
        self.order = tuple(r.record_id for r in self.loaded.records)
        self.by_id = self.loaded.by_id
        self.domains = {}
        self.gap_labels = []
        for rec in self.loaded.records:
            if rec.type == ANCHOR:
                self.domains[rec.record_id] = (utc_ms_candidate(rec.utc_ms),)
            elif rec.local is not None:
                cands = local_candidates(rec.local, self.loaded.zones[rec.stream])
                if not cands:
                    self.gap_labels.append(rec.record_id)
                self.domains[rec.record_id] = cands
            else:
                self.domains[rec.record_id] = (utc_ms_candidate(rec.utc_ms),)
        self.relations = self._build_relations()

    def _build_relations(self):
        rels = []
        by_stream = {}
        for rec in self.loaded.records:
            by_stream.setdefault(rec.stream, []).append(rec)
        for stream, recs in by_stream.items():
            recs.sort(key=lambda r: r.seq)
            for a, b in zip(recs, recs[1:]):
                rels.append(("<=", a.record_id, b.record_id,
                             f"stream {stream} seq {a.seq}<={b.seq}"))
        ends = {}
        for rec in self.loaded.records:
            if rec.type == DOOR:
                ends.setdefault((rec.device, rec.session_id), {}) \
                    .setdefault(rec.kind, []).append(rec)
        for (dev, sid), kinds in sorted(ends.items()):
            for en in kinds.get("enter", []):
                for ex in kinds.get("exit", []):
                    rels.append(("<", en.record_id, ex.record_id,
                                 f"session {dev}/{sid} enter<exit"))
        return rels

    def _valid(self, assignment):
        for op, left, right, _ in self.relations:
            if left not in assignment or right not in assignment:
                continue
            a, b = assignment[left], assignment[right]
            if op == "<=" and not a <= b:
                return False
            if op == "<" and not a < b:
                return False
        return True

    def component_of(self, record_id):
        """Undirected relation-connected component of one record."""
        adj = {rid: set() for rid in self.order}
        for _, left, right, _ in self.relations:
            adj[left].add(right)
            adj[right].add(left)
        seen, stack = set(), [record_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(adj[cur] - seen)
        return seen

    def all_solutions(self):
        """Exhaust every combination; yields each satisfying assignment dict."""
        keys = self.order
        for combo in itertools.product(*(self.domains[k] for k in keys)):
            assignment = dict(zip(keys, combo))
            if self._valid(assignment):
                yield assignment

    def feasible_sets(self):
        """Record_id -> set of candidate ms appearing in at least one solution."""
        feasible = {rid: set() for rid in self.order}
        count = 0
        for assignment in self.all_solutions():
            count += 1
            for rid, ms in assignment.items():
                feasible[rid].add(ms)
        return feasible, count

    def decisions(self):
        """Re-derive the pipeline's session-level export/quarantine rules."""
        feasible, n_solutions = self.feasible_sets()
        sessions = {}
        for rec in self.loaded.records:
            if rec.type != DOOR:
                continue
            key = (rec.device, rec.session_id)
            sessions.setdefault(key, {})[rec.kind] = rec.record_id
        decision = {rid: "anchor" for rid in self.order
                    if self.by_id[rid].type == ANCHOR}
        exported = []
        for (dev, sid), ends in sorted(sessions.items()):
            rids = list(ends.values())
            complete = "enter" in ends and "exit" in ends
            all_det = all(len(feasible[rid]) == 1 for rid in rids)
            if all_det:
                for kind, rid in ends.items():
                    rec = self.by_id[rid]
                    decision[rid] = "exported"
                    exported.append({
                        "device": rec.device, "session_id": rec.session_id,
                        "kind": rec.kind, "ts": next(iter(feasible[rid])),
                        "content": rec.content,
                    })
            else:
                for rid in rids:
                    decision[rid] = ("held_back_partner_ambiguous"
                                     if len(feasible[rid]) == 1
                                     else "quarantined_ambiguous")
        exported.sort(key=lambda e: (e["device"], e["session_id"], e["kind"]))
        return feasible, decision, exported, n_solutions

    def instance_for(self, record_id, candidate_ms):
        """One joint assignment for the whole input where record takes candidate."""
        for assignment in self.all_solutions():
            if assignment[record_id] == candidate_ms:
                return dict(assignment)
        return None
