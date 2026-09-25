# Known gaps and next checkpoint

Updated 25 September 2026 for the 0.2.0 preview selection. Choosing a default does
not erase earlier failures or establish broad reliability.

| Gap | Evidence and current limit | Next acceptance check |
| --- | --- | --- |
| App lifecycle and window recovery | Calendar process/window inventory can remain present while its AX surface is unreadable and its window is invisible/off-Space. Activation was uncertain and repeated reads stopped without input. The user reports the retry worked after dealing with the background process. | Generic running-without-readable-window detection, supported reopen/relist/observe recovery, bounded stopping; complete the requested view change with both visible and hidden starts. Do not kill apps or weaken fresh checks. |
| Useful recovery feedback | Compact results can omit driver diagnosis/remedies and auxiliary-window context, leaving repeated inspection as the model's next choice. | Preserve compact availability/recovery evidence and distinguish process activation from readable target availability. |
| Model decisions and constraints | 27B has limited successful reports, while prior Calculator/Settings runs failed selection or full constraint coverage. Smaller 7B/1.5B remain available but unqualified. | Frozen ordinary-language Calendar/browser tasks through the installed CLI; inspect the first consequential failure instead of tuning fixtures. |
| Latency | Several native runs exceeded two minutes; the failed Calendar run took 125.42s, six calls and 30,031 input tokens. | Measure time excluding human review, calls and tokens on verified completions; the target remains at least 8/10 per workflow, no unintended changes, typical tasks under two minutes. |
| Multiple displays and cursor | The user confirmed a cursor hold was visible; the driver captures display topology at startup. Dynamic rearrangement/hotplug is unproved. | Verify cursor and fresh target geometry on each active display and after topology changes. |
| Browser and Office transfer | A browser execution demo used a supplied reviewed plan; its ordinary-language planning attempt failed. That execution demo does not prove language autonomy. Fresh Excel/PowerPoint file/action transfer is not established. | Complete a fresh browser task through the same plain-language entry, then disposable Office file/actions; keep adapter-specific evidence explicit. |
| Saved output and preservation | Exact buffer readback is distinct from saved bytes. Generic native tasks block unsupported required/forbidden backing-file changes. | Add a reusable persistence capability before claiming saved-file or no-file-change success. |
| Platforms and RLCD | Current runtime is Apple Silicon/MLX. Windows/Linux are unverified. Default ordinary tool calling is distinct from retained original RLCD. | Return to a measured RLCD comparison after functional acceptance; preserve model and decoding labels. |

The immediate engineering checkpoint is **generic window lifecycle recovery plus
an installed-CLI Calendar regression**, followed by one browser variation. Launch
or observation alone does not count. Test visible, running-without-visible-window,
and ambiguous/unavailable cases; require final outcome verification. Do not
resume TextEdit testing under the current demo scope.

The 0.2.0 release work checks configuration routing, packaging and compatibility.
It does not claim a new autonomous desktop completion. The latest successful
Calendar behavior is user-reported. Prior failed local traces remain private and
retained; this document deliberately contains no personal UI content or credentials.
