# Task branch: Common Pitfalls

Read this branch only for common pitfalls. Original content below is verbatim. Resolve its embedded relative paths from the skill directory (the parent of references/), as in the original SKILL.md.

## Common Pitfalls

1. **Blaming the model from total wall clock.** First separate call count, context growth, tools, and orchestration.
2. **Comparing unlike harnesses.** Different CWD, auth, tier, reasoning, cache state, or completion criteria invalidates causal claims.
3. **Calling a test suite a latency result.** Tests establish safety, not user-visible speed.
4. **Optimizing the first error instead of task completion.** Measure both first useful answer and final verified answer.
5. **Lowering compression thresholds first.** This can replace prompt bloat with compression thrash.
6. **Keeping all skill and tool text “just in case.”** Progressive disclosure is safer than paying the full context cost every round.
7. **Hard-stopping complex tasks.** Promote to durable work rather than reporting a partial result as done.
8. **Tuning hardware before loops.** More resources cannot eliminate unnecessary sequential reasoning rounds.
9. **Treating anecdotes as prevalence.** Public issues are negatively selected and showcases positively selected; report that limitation.
10. **Attributing a direct sample to durable roles.** A direct gateway p50 or model-call count says nothing causal about a Kanban graph unless that graph produced the sample.
11. **Stacking an executor beneath an existing role graph.** `Coordinator → implementer → coding CLI → implementer recheck → verifier → coordinator recheck` adds handoffs and duplicate verification. Keep the CLI internal to the implementer when used.
12. **Correcting only the parent of a dispatched graph.** Already-created implementation and QA children retain stale acceptance criteria. Steer every affected task and require acknowledgement from active workers.
13. **Treating a fallback entry as failure-domain isolation.** Provider and model labels are not evidence; only the resolved endpoint origin is. A same-origin "fallback" turns one terminal error into N amplifying retries.
14. **Reading a watchdog "skipped" counter as a failure count.** Skips over requests with a live response id and streamed events are correct behavior. Fixing the counter by evicting on age kills healthy long inferences.
15. **Raising a stale timeout to hide queue saturation.** Longer timeouts extend capacity waiting too. Bound the wait at the upstream's admission layer instead.
16. **Deploying config, agent code, and upstream service changes together.** You then cannot attribute either the improvement or the regression. Stage them and restart each service once, at its own stage.
17. **Committing a reliability fix out of a dirty shared worktree.** A long-lived worktree accumulates unrelated modified/untracked files; `git status --short | wc -l` far exceeding your own touched set means a blanket commit would ship someone else's work. Enumerate the incident paths explicitly, report the intended PR scope for approval, and only then commit — verification passing is not authority to push or deploy.
18. **Reusing an independent review verdict across revisions.** A PASS describes the revision that was reviewed. After fixing reviewer findings, re-run the review on the new source; state which findings went RED → GREEN.
19. **Treating tool-progress cards as user-visible output.** A thread full of command echoes can have zero confirmed deliveries. Output-silence watchdogs count platform ACKs for assistant content only; count adapter flush lines, not what the channel looks like.
20. **Diagnosing a watchdog kill from the wrong checkout.** Sibling clones of the runtime exist; resolve the live path from the service `ExecStart` before reading or patching source, or the analysis describes code that is not running.
21. **Raising the first concurrency limit you find.** Measured peak concurrency names the *binding* gate, not the only one. A per-command semaphore and a host-global lock acquired in the same statement both have to move, or the projected speedup silently re-binds at the lower one.
22. **Proposing a content-shaped fix for a position-shaped failure.** When items dispatch in waves, late items fail from queue position. Prompt caps and exploration budgets do nothing — observed: the batch's *lightest* item succeeded at offset 735 s and timed out at 2549 s with the same content and a 3× larger deadline.
23. **Blaming the category that late failures happen to share.** Compare per-category means first; the accused category was the fastest of three (1336 s vs 1399 s vs 1440 s).
24. **Reporting a correctness fix as if it answered a latency request.** State the original objective verbatim and say explicitly when latency is unchanged or worse.


