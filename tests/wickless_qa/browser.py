"""Optional real Chromium UX test. No broker, external asset or remote service required."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import expect, sync_playwright

from bot.wickless.config import Config, load


def run(url):
    with sync_playwright() as pw, TemporaryDirectory() as tmp:
        b = pw.chromium.launch(args=["--no-sandbox"])
        ctx = b.new_context(
            viewport={"width": 1340, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        assert page.title().startswith("Wickless Desk")

        def btn(name):
            return page.get_by_role("button", name=name, exact=True)

        def select(s):
            return page.get_by_role("checkbox", name="Select " + s, exact=True)

        assert page.locator("#pairs tr").count() == 6
        select("EURUSDm").uncheck()
        select("USDJPYm").check()
        btn("Copy pairs").click()
        assert page.evaluate("navigator.clipboard.readText()") == "GBPUSDm, USDJPYm"
        lots = page.get_by_role("spinbutton", name="Lots for USDJPYm", exact=True)
        lots.fill("0.05")
        lots.press("Tab")
        page.reload()
        expect(lots).to_have_value("0.05")
        btn("Edit USDJPYm").click()
        page.get_by_label("Risk cap (USD)").fill("12")
        btn("Save instrument").click()
        btn("Add pairs").click()
        page.get_by_label("Symbols", exact=True).fill("GBPUSDm, EURUSD.a")
        btn("Add to selection").click()
        assert page.locator("#pairs tr").count() == 7
        btn("Copy setup for desktop agent").click()
        setup = page.evaluate("navigator.clipboard.readText()")
        assert setup.splitlines()[0] == "WICKLESS OBSERVE"
        c = Config.model_validate_json(setup.split("\n", 1)[1])
        assert [p.symbol for p in c.pairs] == ["GBPUSDm", "USDJPYm", "EURUSD.a"]
        assert c.pairs[1].max_risk_usd == 12
        with page.expect_download() as d:
            btn("Download bot config").click()
        f = Path(tmp) / d.value.suggested_filename
        d.value.save_as(f)
        assert f.name == "wickless.local.json" and load(f) == c
        btn("Config JSON").click()
        assert Config.model_validate_json(page.locator("#code").inner_text()) == c
        btn("Commands").click()
        assert (
            "--arm" not in page.locator("#code").inner_text()
            and "preflight" in page.locator("#code").inner_text()
        )
        btn("+ New broker").click()
        page.get_by_label("Profile name").fill("Broker Two")
        page.get_by_label("Exact MT5 server (optional in paper)").fill("Alternate-Demo")
        page.get_by_label("Retest window (bars)").fill("5")
        btn("Save profile").click()
        expect(btn("Download bot config")).to_be_disabled()
        btn("Add pairs").click()
        page.get_by_label("Symbols", exact=True).fill("US30.cash XAUUSD#")
        btn("Add to selection").click()
        btn("Edit US30.cash").click()
        btn("Remove pair").click()
        assert page.locator("#pairs tr").count() == 1
        btn("Import config").click()
        page.get_by_label("Wickless config JSON").fill('{"not":"a profile"}')
        btn("Import profile").click()
        expect(page.locator("#derr")).to_be_visible()
        page.get_by_label("Wickless config JSON").fill(f.read_text())
        btn("Import profile").click()
        assert (
            page.locator("#brokerSelect option").count() == 3
            and page.locator("#pairs tr").count() == 3
        )
        btn("Rulebook").click()
        expect(page.locator("#rulebook")).to_be_visible()
        btn("Back to workspace").click()
        page.locator("#brokerSelect").select_option("0")
        expect(select("EURUSDm")).not_to_be_checked()
        expect(lots).to_have_value("0.05")
        page.set_viewport_size({"width": 390, "height": 820})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        btn("Add pairs").click()
        expect(page.get_by_label("Symbols", exact=True)).to_be_visible()
        btn("Cancel").click()
        assert not errors, errors
        # Static-file operation with blocked storage/clipboard still permits a manual copy.
        ctx2 = b.new_context()
        p2 = ctx2.new_page()
        p2.add_init_script(
            "Object.defineProperty(window,'localStorage',{get(){throw Error('denied')}}); Object.defineProperty(navigator,'clipboard',{value:undefined}); document.execCommand=()=>false"
        )
        p2.goto(url)
        p2.get_by_role("button", name="Copy pairs", exact=True).click()
        expect(p2.get_by_role("dialog")).to_be_visible()
        assert "EURUSDm" in p2.locator("dialog textarea").input_value()
        b.close()
    print(
        "PASS: Chromium desktop + mobile, profiles, editable lots/risk, pair selection, clipboard & manual fallback, reload, safe import, schema-validated config download, observer commands, and rulebook."
    )


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8035")
