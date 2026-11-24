"""用 Playwright 驱动页面，检查渲染、交互与推演播放。"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5055"
errors = []
console = []


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1600, "height": 950})
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1500)

        # header populated
        svc = page.inner_text("#stat-services")
        edges = page.inner_text("#stat-edges")
        amp = page.inner_text("#stat-amp")
        inv = page.inner_text("#stat-inverted")
        print(f"header: services={svc} edges={edges} amp={amp} inverted={inv}")

        nodes = page.locator("#graph .node").count()
        edge_els = page.locator("#graph .edge").count()
        print(f"graph: nodes={nodes} edges={edge_els}")
        page.screenshot(path="/tmp/shot_initial.png")

        # click an incident edge -> config panel
        page.click("#graph .edge[data-id='order-bff-01->order-svc-01']")
        page.wait_for_timeout(800)
        panel = page.inner_text("#panel")
        assert "合并规则" in panel, "config panel missing rules"
        assert "8" in panel, "attempts missing"
        print("config panel OK")
        page.screenshot(path="/tmp/shot_config.png")

        # inversions tab
        page.click('.tabs button[data-tab="inversions"]')
        page.wait_for_timeout(800)
        ptext = page.inner_text("#panel")
        assert "倒挂" in ptext
        print("inversions tab OK, rows:", page.locator("#panel tr[data-edge]").count())

        # amplification tab
        page.click('.tabs button[data-tab="amplification"]')
        page.wait_for_timeout(800)
        assert "放大" in page.inner_text("#panel")
        print("amplification tab OK")

        # reproduce incident
        page.click("#btn-incident")
        page.wait_for_timeout(3000)
        frame_label = page.inner_text("#frame-label")
        print("playback:", frame_label)
        page.screenshot(path="/tmp/shot_incident_t0.png")

        # play through
        page.click("#btn-play")
        page.wait_for_timeout(2500)
        label2 = page.inner_text("#frame-label")
        print("after play:", label2)
        page.click("#btn-play")  # pause
        # jump to end
        page.eval_on_selector("#tl-range", "el => { el.value = el.max; el.dispatchEvent(new Event('input')); }")
        page.wait_for_timeout(800)
        print("at end:", page.inner_text("#frame-label"))
        page.screenshot(path="/tmp/shot_incident_end.png")

        # simulation panel
        page.click('.tabs button[data-tab="simulation"]')
        page.wait_for_timeout(500)
        simtext = page.inner_text("#panel")
        assert "被打到容量上限" in simtext
        print("simulation panel OK")
        page.screenshot(path="/tmp/shot_sim_panel.png")

        # proposals + evaluate
        page.click('.tabs button[data-tab="proposals"]')
        page.wait_for_timeout(1500)
        assert "修改建议" in page.inner_text("#panel")
        btns = page.locator("#panel button[data-eval]")
        print("proposal buttons:", btns.count())
        page.screenshot(path="/tmp/shot_proposals.png")
        # click combo evaluate
        page.click("#panel button[data-eval='combo']")
        page.wait_for_timeout(6000)
        ev = page.inner_text("#eval-combo")
        print("combo eval:", ev[:120].replace("\n", " | "))
        page.screenshot(path="/tmp/shot_combo_eval.png")

        # verify tab
        page.click('.tabs button[data-tab="verify"]')
        page.wait_for_timeout(4000)
        vtext = page.inner_text("#panel")
        assert "三方一致" in vtext or "存在偏差" in vtext
        print("verify tab:", "一致" if "三方一致" in vtext else "偏差")
        page.screenshot(path="/tmp/shot_verify.png")

        b.close()

    print("\n=== JS errors ===")
    for e in errors:
        print("ERR:", e)
    print("=== console ===")
    for c in console[:20]:
        print(c)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
