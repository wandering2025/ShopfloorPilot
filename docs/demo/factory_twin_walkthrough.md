# Factory twin demonstration (machine breakdown)

About five minutes, offline, no API key. It shows the breakdown workflow visually:
normal running, a failure that interrupts work, an approved replan that moves work
to other machines, an explicit maintenance confirmation, and completion. Design and
limits: [factory twin architecture](../architecture/factory_twin.md).

## Launch

```bash
SHOPFLOOR_PILOT_UI_RUNTIME_DIR=.runtime/twin-demo \
  uv run --offline streamlit run src/shopfloor_pilot/ui/app.py \
  --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501` in a browser. If WebGL is unavailable, the
view falls back to 2D automatically; you can also choose **Renderer → 2D (no WebGL)**.

## Steps

1. **Reset.** In the sidebar choose **Recoverable machine breakdown**, then
   **Start fresh run / reset**.
2. **Initial plan.** In **Agent workflow & approval** keep **Deterministic baseline**
   and **Plain plan / replan**, press **Submit request**, review, then **Approve &
   apply this proposal**.
3. **Watch normal production.** Open **Factory twin**. Set **Speed** to 300× if you
   want a shorter story. Press **Inject fixture fault and watch**. Playback runs from
   the plan to 2,700 s: green machines and progress bars, units moving from queues to
   machines to finished goods. It holds on the key event: **CNC02 FAILED**, and
   `order-001#1` step 1 interrupted with 180 s wasted, held beside CNC02 (red outline).
   Point out that the CNC02 card says no work can start until maintenance is confirmed.
4. **Recovery decision.** In **Agent workflow & approval** choose **Recorded agent
   session (offline replay)** (labeled as recorded, not live DeepSeek) and **Abnormality report / recovery**,
   submit, review and approve. Back in **Factory twin**, the view holds on *Approved
   plan revision 2 applied*: purple arcs and "↪ from CNC02" badges show the changed
   assignments; the interrupted unit is now queued on CNC03 as a restart.
   CNC02 is still FAILED — plan approval is not repair approval.
5. **Production without CNC02.** Press **Continue production without CNC02 and
   watch**. The approved plan runs for as long as the normal segment before the fault
   (2,700 → 5,400 s, about 23 s at 120×) while CNC02 stays **FAILED** with its red
   zone: CNC03 runs the full-duration restart of `order-001#1` under the purple arc,
   then the work moved from CNC02; CNC01 and QC01 continue. The restart's arc
   disappears when the restart completes. Simulated time moves only because you
   pressed the button. (Before the recovery plan is approved, this button and **Run
   plan to completion** are disabled: the old plan still has work on CNC02.)
6. **Maintenance.** Press **Confirm CNC02 maintenance complete**. The card turns
   **RESTORED · IDLE** (an explicit `machine_restored` event at 5,401 s) and the
   remaining reassignment arcs disappear, because the machine they avoided is back.
7. **Finish the plan.** Press **Run plan to completion and watch** (to 6,360 s, every
   unit reaches finished goods). Optionally set **Advance by** to the remaining
   horizon and press **Advance and watch**; the projection check then shows
   **✓ Visual projection matches authoritative SQLite state v33 at 28,800 s**.

Use **⏮ From start** to replay the whole run, **◀ Prev / Next ▶** to step through
committed event groups, or the slider to scrub. Untick **Hold at key events** for an
uninterrupted run.

## What to say

- Everything shown is replayed from the committed SQLite event groups; the view
  cannot write, approve or restore. The header compares playback and live versions.
- Cards, scene and event list share one playback clock.
- Movement between stations is a visual convention; transport is not modeled.
- At the end, the browser checks what it displays against the stored state.

