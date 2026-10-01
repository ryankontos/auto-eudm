"""Browser regressions against an isolated simulator (never a live Helix server).

Example: .venv/bin/python scripts/check_alm_ui.py --url http://127.0.0.1:8778
This resets that simulator's queue/drafts. Requires optional Playwright + Chrome.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
import io
from pathlib import Path
import unittest

from playwright.sync_api import expect, sync_playwright


class ALMBrowserChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=OPTIONS.chrome)
        cls.client = cls.playwright.request.new_context(base_url=OPTIONS.url)
        if not cls.client.get("/api/config").json().get("simulation"):
            cls.browser.close()
            cls.playwright.stop()
            raise RuntimeError("These checks may only reset an isolated simulator.")

    @classmethod
    def tearDownClass(cls):
        cls.client.dispose()
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        current = self.client.get("/api/queue").json().get("requests", [])
        self.client.post("/api/queue", data={"requests": [], "base_requests": current})
        for draft in self.client.get("/api/import-drafts").json().get("drafts", []):
            self.client.delete(f"/api/import-drafts/{draft['id']}")
        self.client.delete("/api/import/backlog/ignored")
        self.columns = {
            "username": "Username", "deployment_serial": "SN",
            "returned_device": "Returned Device SN", "pending_return": "OLD Device SN",
            "old_device_serial": "", "return_checkbox": "", "enabled": "Attended",
            "device_allocation": "Device(s) Allocation", "new_asset_status": "New Asset Status",
            "first_name": "First Name", "last_name": "Last Name",
        }
        self.client.post("/api/preferences", data={"import_columns": self.columns,
            "pc_toolkit_enabled": False, "save_alm_import_drafts": True})
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.open()

    def tearDown(self):
        if OPTIONS.screenshots and self.page.locator("#importDialog[open]").count():
            self.page.screenshot(path=str(OPTIONS.screenshots / f"{self._testMethodName}.png"))
        self.context.close()
        self.assertEqual(self.errors, [], "Browser JavaScript errors")

    def open(self):
        self.page.goto(OPTIONS.url, wait_until="domcontentloaded")
        expect(self.page.locator("body.app-ready")).to_be_visible(timeout=15000)

    def rows(self, extra=None):
        today = date.today().strftime("%d/%m/%Y")
        return [
            [today, "jane.smith", "C02NEW12345", "", "C02OLD12345", "TRUE", "MacBook Air M2", "In Inventory", "Jane", "Smith", ""],
            [today, "alex.chen", "C02NEW23456", "C02OLD23456 PR", "", "TRUE", "MacBook Pro M3", "In Inventory", "Alex", "Chen", ""],
        ] + (extra or [])

    def upload(self, rows=None, backlog=False):
        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow(["Date", "Username", "SN", "Returned Device SN", "OLD Device SN", "Attended",
            "Device(s) Allocation", "New Asset Status", "First Name", "Last Name", "Notes"])
        writer.writerows(self.rows() if rows is None else rows)
        self.page.locator("#almBacklogButton" if backlog else "#importSheetButton").click()
        self.page.locator("#workbookInput").set_input_files({"name": "browser-check.csv",
            "mimeType": "text/csv", "buffer": text.getvalue().encode()})
        expect(self.page.locator("#importConfigure")).to_be_visible(timeout=30000)

    def review(self):
        self.page.locator("#prepareImportButton").click()
        expect(self.page.locator("#importPreview")).to_be_visible(timeout=30000)

    def verified(self):
        count = self.page.evaluate("() => state.importPreview.requests.filter(r => r.included !== false).length")
        expect(self.page.locator('#importPreviewList [data-validation-state="valid"]')).to_have_count(count, timeout=20000)

    def statuses(self):
        for select in self.page.locator("[data-import-status]").all():
            group = select.get_attribute("data-import-status")
            status = self.page.evaluate("id => state.importPreview.requests.find(r => r.id === id).group", group)
            select.select_option("Deployed - New Stock" if status == "Deployments" else "Pending Rebuild")

    def test_original_csv_and_status_suffix(self):
        self.upload()
        expect(self.page.locator("#importSheetField")).to_be_hidden()
        self.review()
        self.verified()
        result = self.page.evaluate("() => state.importPreview.requests.map(r => ({group:r.group,serial:r.serials[0],status:r.status}))")
        returned = next(row for row in result if row["group"] == "Returned devices")
        self.assertEqual(returned["serial"], "C02OLD23456")
        self.assertEqual(returned["status"], "Pending Rebuild")
        self.assertNotIn("invalid", self.page.locator("#importPrepareHint").inner_text().lower())

    def test_combined_columns_search_picker_and_mapping_resume(self):
        self.upload()
        self.page.locator("#changeColumnsButton").click()
        self.page.locator("#importReturnLayoutInput").select_option("combined")
        self.page.evaluate("() => {document.querySelector('#importMapOldDevice').tomselect.setValue('OLD Device SN'); document.querySelector('#importMapReturnCheckbox').tomselect.setValue('Attended'); document.querySelector('#importMapEnabled').tomselect.setValue('');}")
        expect(self.page.locator('[data-import-layout="separate"]').first).to_be_hidden()
        self.page.evaluate("() => saveCurrentImportDraft({immediate:true})")
        self.page.reload(wait_until="domcontentloaded")
        expect(self.page.locator("body.app-ready")).to_be_visible()
        self.page.locator("#resumeLatestImportButton").click()
        expect(self.page.locator("#importMapColumns")).to_be_visible()
        self.assertEqual(self.page.locator("#importReturnLayoutInput").input_value(), "combined")
        self.assertEqual(self.page.locator("#importMapOldDevice").evaluate("s=>s.tomselect.getValue()"), "OLD Device SN")
        self.page.locator("#prepareImportButton").click()
        expect(self.page.locator("#importConfigure")).to_be_visible()
        self.review()
        self.verified()
        self.assertEqual(self.page.evaluate("()=>state.importPreview.requests.filter(r=>r.group==='Returned devices').length"), 1)

    def test_review_refresh_preserves_input_and_handlers(self):
        self.upload()
        self.review()
        field = self.page.locator("[data-import-return-serial]").first
        field.fill("EDITING123")
        result = field.evaluate("input=>{input.focus();input.setSelectionRange(2,5);window.testInput=input;renderImportPreview();renderImportPreview();return {same:document.activeElement===input,value:input.value,start:input.selectionStart,end:input.selectionEnd};}")
        self.assertEqual(result, {"same": True, "value": "EDITING123", "start": 2, "end": 5})
        # A preserved checkbox must still have exactly one current handler.
        checkbox = self.page.locator("[data-import-include]").first
        checkbox.uncheck()
        self.assertFalse(self.page.evaluate("()=>state.importPreview.requests[0].included"))

    def test_return_conversion_and_undo_redo(self):
        self.upload()
        self.review()
        self.verified()
        control = self.page.locator('[data-import-return-kind]').filter(has=self.page.locator('option[value="pending_returns"][selected]')).first
        request_id = control.get_attribute("data-import-return-kind")
        control.select_option("returned_devices")
        self.assertEqual(self.page.evaluate("id=>state.importPreview.requests.find(r=>r.id===id).returning_user", request_id), "jane.smith")
        self.page.locator("#undoImportButton").click()
        self.assertEqual(self.page.evaluate("id=>state.importPreview.requests.find(r=>r.id===id).group", request_id), "Pending returns")
        self.page.locator("#redoImportButton").click()
        self.assertEqual(self.page.evaluate("id=>state.importPreview.requests.find(r=>r.id===id).group", request_id), "Returned devices")
        current = self.page.locator(f'[data-import-return-kind="{request_id}"]')
        current.focus()
        self.page.evaluate("()=>undoImportEdit()")
        expect(self.page.locator(f'[data-import-return-kind="{request_id}"]')).to_have_value("pending_returns")

    def test_review_actions_stay_visible_in_small_windows(self):
        self.upload()
        self.review()
        for width, height in [(960, 720), (640, 720), (1280, 600)]:
            self.page.set_viewport_size({"width": width, "height": height})
            if OPTIONS.screenshots:
                self.page.screenshot(path=str(OPTIONS.screenshots / f"review-{width}-{height}.png"), animations="disabled")
            result = self.page.evaluate("""() => {
                const actions = document.querySelector('#prepareImportButton').getBoundingClientRect();
                const list = document.querySelector('#importPreviewList');
                return {visible:actions.top >= 0 && actions.bottom <= innerHeight,
                    fits:list.scrollWidth <= list.clientWidth, scrolls:list.scrollHeight > list.clientHeight};
            }""")
            self.assertEqual(result, {"visible": True, "fits": True, "scrolls": True})

    def test_missing_returns_new_joiner_and_missing_username(self):
        today = date.today().strftime("%d/%m/%Y")
        self.upload(rows=[
            [today,"normal.user","NEWNORMAL1","","","TRUE","Air M2","In Inventory","Normal","User",""],
            [today,"starter.user","NEWSTARTER1","","","TRUE","Air M3","In Inventory","New","Starter","NeW   JoInEr"],
            [today,"","NEWNOUSER1","","","TRUE","Air M4","In Inventory","Missing","Name",""],
        ])
        self.review()
        expect(self.page.locator("[data-import-manual-source]")).to_have_count(1)
        expect(self.page.locator(".import-new-joiner")).to_have_text("New joiner")
        self.page.locator("[data-import-missing-user]").fill("fixed.user")
        self.page.locator("[data-import-missing-user]").press("Enter")
        self.assertTrue(self.page.locator("#importDialog").evaluate("d=>d.open"))
        self.assertTrue(self.page.evaluate("()=>state.importPreview.requests.some(r=>r.user==='fixed.user')"))
        self.page.locator("[data-import-manual-serial]").first.fill("RETURNMAN1")
        self.page.locator("[data-import-manual-add]").first.click()
        self.assertTrue(self.page.evaluate("()=>state.importPreview.requests.some(r=>r.serials[0]==='RETURNMAN1')"))
        expect(self.page.locator(".import-return-resolved").filter(has_text="Return manually added")).to_have_count(1)

    def test_failed_queue_save_retains_draft_then_success_clears_it(self):
        self.upload()
        self.review()
        self.verified()
        self.statuses()
        self.page.route("**/api/queue", lambda route: route.fulfill(status=503, json={"error":"Simulated save failure"}) if route.request.method == "POST" else route.continue_())
        self.page.locator("#prepareImportButton").click()
        expect(self.page.locator("#importError")).to_contain_text("Your import has been kept")
        self.assertTrue(self.client.get("/api/import-drafts").json()["drafts"])
        self.page.unroute("**/api/queue")
        self.page.locator("#prepareImportButton").click()
        expect(self.page.locator("#importDialog")).not_to_be_visible(timeout=20000)
        queued = self.client.get("/api/queue").json()["requests"]
        self.assertEqual(len(queued), 4)
        self.assertEqual(self.client.get("/api/import-drafts").json()["drafts"], [])
        self.assertTrue(all(" PR" not in serial and " PD" not in serial for row in queued for serial in row["serials"]))

    def test_individual_pc_retry_does_not_cancel_other_rows(self):
        self.upload()
        self.review()
        result = self.page.evaluate("""async () => {
          state.preferences.pc_toolkit_enabled = true;
          const payload = state.importPreview;
          const rows = payload.requests.filter(r => r.group === 'Deployments');
          const original = pcToolkitEnrichImportQueries;
          const runs = [];
          pcToolkitEnrichImportQueries = queries => new Promise(resolve => runs.push({queries,resolve}));
          try {
            const full = enrichImportPreview(payload);
            const retry = enrichImportPreview(payload, {requests:[rows[0]],fresh:true});
            const response = run => ({results:Object.fromEntries(run.queries.map(q=>[pcToolkitKey(q),{found:true,primary:{serial:q,model:'Test MacBook',status:'In Inventory'},devices:[]}]))});
            runs[1].resolve(response(runs[1])); await retry;
            runs[0].resolve(response(runs[0])); await full;
            return rows.map(r=>({loading:r.pc_toolkit_loading,model:pcToolkitModelFor(r),serials:pcToolkitImportSerials(r)}));
          } finally {pcToolkitEnrichImportQueries=original;state.preferences.pc_toolkit_enabled=false;}
        }""")
        self.assertEqual(len(result), 2)
        self.assertTrue(all(not row["loading"] and row["model"] == "Test MacBook" for row in result), result)
        self.assertEqual(result[0]["serials"], ["C02NEW12345"])

    def test_backlog_dates_duplicate_users_and_exclusions(self):
        yesterday = (date.today()-timedelta(days=1)).strftime("%d/%m/%Y")
        rows = self.rows()
        rows[0][0] = rows[1][0] = yesterday
        rows[1][1] = rows[0][1]
        self.upload(rows=rows, backlog=True)
        self.review()
        self.verified()
        expect(self.page.locator("[data-backlog-include]")).to_have_count(2)
        expect(self.page.locator(".import-duplicate-warning")).to_contain_text("2nd occurrence")
        self.page.locator("[data-backlog-include]").first.uncheck()
        self.page.evaluate("()=>saveCurrentImportDraft({immediate:true})")
        self.page.reload(wait_until="domcontentloaded")
        expect(self.page.locator("body.app-ready")).to_be_visible()
        self.page.locator("#resumeLatestImportButton").click()
        expect(self.page.locator("[data-backlog-include]").first).not_to_be_checked()
        self.assertEqual(self.page.evaluate("()=>state.importPreview.mode"), "backlog")

    def test_excel_dates_colours_and_pd_suffix(self):
        from openpyxl import Workbook
        from openpyxl.styles import Font
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Deployments"
        sheet.append(["Date","Username","SN","Returned Device SN","OLD Device SN","Attended"])
        sheet.append([date.today(),"green.user","NEWGREEN1","OLDGREEN1","",True])
        sheet.append([date.today(),"blue.user","NEWBLUE1","OLDBLUE1","",True])
        sheet.append([date.today(),"suffix.user","NEWSUFFIX1","OLDSUFFIX1 PD","",True])
        sheet.append([date.today()-timedelta(days=1),"yesterday.user","NEWYEST1","OLDYEST1","",True])
        sheet["D2"].font = Font(color="FF00B050")
        sheet["D3"].font = Font(color="FF000080")
        buffer = io.BytesIO()
        workbook.save(buffer)
        self.page.locator("#importSheetButton").click()
        self.page.locator("#workbookInput").set_input_files({"name":"colours.xlsx","mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","buffer":buffer.getvalue()})
        expect(self.page.locator("#importConfigure")).to_be_visible(timeout=30000)
        self.assertEqual(self.page.evaluate("()=>selectedImportDates()"), [date.today().isoformat()])
        self.page.locator("#openImportDatesButton").click()
        for checkbox in self.page.locator("[data-import-dialog-date]").all():
            checkbox.check()
        self.page.locator("#applyImportDatesButton").click()
        self.assertEqual(len(self.page.evaluate("()=>selectedImportDates()")), 2)
        self.review()
        self.verified()
        returned = self.page.evaluate("()=>Object.fromEntries(state.importPreview.requests.filter(r=>r.group==='Returned devices').map(r=>[r.serials[0],r.status]))")
        self.assertEqual(returned["OLDGREEN1"], "Pending Rebuild")
        self.assertEqual(returned["OLDBLUE1"], "Pending Decom")
        self.assertEqual(returned["OLDSUFFIX1"], "Pending Decom")

    def test_verification_correction_is_saved_before_retry(self):
        self.page.route("**/api/search/assets", lambda route: route.fulfill(json={"results":[]}) if route.request.post_data_json.get("query") == "C02NEW12345" else route.continue_())
        self.upload()
        self.review()
        field = self.page.locator("[data-import-serial]")
        expect(field).to_be_visible(timeout=20000)
        expect(self.page.locator("[data-import-user]")).to_have_count(0)
        field.fill("CORRECTED123")
        field.press("Tab")
        self.page.evaluate("()=>saveCurrentImportDraft({immediate:true})")
        self.page.reload(wait_until="domcontentloaded")
        expect(self.page.locator("body.app-ready")).to_be_visible()
        self.page.locator("#resumeLatestImportButton").click()
        expect(self.page.locator("[data-import-serial]")).to_have_value("CORRECTED123")
        self.page.locator("[data-import-retry]").click()
        self.verified()

    def test_cancelled_prepare_cannot_replace_a_new_import(self):
        self.upload()
        result = self.page.evaluate("""async()=>{
          const original=api;
          let finish;
          api=(path,options)=>path==='/api/import/prepare' ? new Promise(resolve=>finish=resolve) : original(path,options);
          try {
            const work=prepareImport();
            document.querySelector('#importDialog').close();
            resetImportDialog('deploy');
            document.querySelector('#importDialog').showModal();
            finish({requests:[],counts:{},dates:[]});
            await work;
            return {preview:state.importPreview,choose:!document.querySelector('#importChoose').hidden};
          } finally {api=original;}
        }""")
        self.assertEqual(result, {"preview":None,"choose":True})

    def test_duplicate_queue_device_explains_blocked_action(self):
        self.upload()
        self.review()
        self.verified()
        self.statuses()
        self.page.evaluate("()=>{state.queue=[{id:'already-queued',kind:'user',serials:['C02NEW12345'],status:'Deployed - New Stock',user:'jane.smith'}];renderImportPreview();updateImportPrepareButton();}")
        expect(self.page.locator("#importPrepareHint")).to_contain_text("Already in the queue")
        expect(self.page.locator("#prepareImportButton")).to_be_disabled()
        self.page.locator("[data-import-include]").first.uncheck()
        expect(self.page.locator("#prepareImportButton")).to_be_enabled()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="An isolated simulator; its queue/drafts will be reset")
    parser.add_argument("--chrome", default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--tests", nargs="*", help="Optional unittest names to run")
    OPTIONS = parser.parse_args()
    if OPTIONS.screenshots:
        OPTIONS.screenshots.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[__file__, *(OPTIONS.tests or [])], verbosity=2)
