"""parse_menu against REAL Claude Code screens captured on gus (2026-10-09): single choice, multi-select with the
cursor on an option, and a multi-question wizard with the cursor on the unnumbered Next row. Run: python3 tests/menu_parse_test.py"""
import importlib.util, os, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)

SINGLE = """Do you want to proceed?
❯ 1. Yes
  2. Yes, and do not ask again
  3. No

Esc to cancel"""

MULTI_ON_OPTION = """Pick any

❯ 1. [ ] Alpha
         First option in the list.
  2. [✔] Bravo
         Second option in the list.
  3. [ ] Charlie
         Third option in the list.
  4. [ ] Delta
         Fourth option in the list.
  5. [ ] Type something
     Submit
────────────────────────────────────────────────────────────────────────────────────────
  6. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel"""

WIZARD_ON_NEXT = """←  ☒ Capture in  ☐ Lists  ☐ Picks kinds  ☐ Groceries  ✔ Submit  →

│ Capture entry points: how should you get things INTO the app? (pick all you want)

  1. [ ] Hyprland hotkey capture (Recommended)
         A key on gus records, transcribes locally.
  2. [✔] iPhone Action Button / Shortcut
         A Shortcut that posts a recording.
  3. [✔] Clipboard / URL capture
         Paste or send a link.
  4. [ ] PWA only
         Keep just the stock phone and desktop browser capture bar.
  5. [ ] Type something
❯    Next
────────────────────────────────────────────────────────────────────────────────────────
  6. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""

REVIEW = """Review your answers

 ● Pick any
   → Bravo

Ready to submit your answers?

❯ 1. Submit answers
  2. Cancel"""

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

m = srv.parse_menu(SINGLE)
check("single: 3 options, cursor on 1, not multi", m and len(m["options"]) == 3 and m["current"] == 1 and not m["multi"] and m["cur_idx"] == 0)
check("single: 'Yes' approves, 'No' does not", m["options"][0]["approves"] and not m["options"][2]["approves"])

m = srv.parse_menu(MULTI_ON_OPTION)
check("multi: detected, cursor on option 1", m and m["multi"] and m["current"] == 1 and m["cur_idx"] == 0)
check("multi: Bravo is the only tick", [o["n"] for o in m["options"] if o["checked"]] == [2])
check("multi: labels lose the [ ] marker", m["options"][0]["label"] == "Alpha")
check("multi: 'Type something' is flagged free", m["options"][4]["free"] and not m["options"][0]["free"])
check("multi: Submit is the row after option 5 (idx 5), Chat about this after it (idx 6)", m["action"]["label"] == "Submit" and m["action"]["idx"] == 5 and m["options"][5]["n"] == 6 and m["options"][5]["idx"] == 6)

m = srv.parse_menu(WIZARD_ON_NEXT)
check("wizard: cursor on the unnumbered Next row is still found", m and m["multi"] and m["cur_idx"] == 5 and m["current"] == 0 and m["action"]["label"] == "Next" and m["action"]["focused"])
check("wizard: options 2 and 3 are ticked", [o["n"] for o in m["options"] if o["checked"]] == [2, 3])

m = srv.parse_menu(REVIEW)
check("review screen is plain single choice", m and not m["multi"] and [o["n"] for o in m["options"]] == [1, 2])

FREE = """Pick one

❯ 1. Alpha
     Choose Alpha.
  2. Bravo
     Choose Bravo.
  3. Type something.
─────────────────────────────
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel"""
check("question: a permission dialog's question is the line above its options", srv.parse_menu(SINGLE)["question"] == "Do you want to proceed?")
check("question: a multi-select's question", srv.parse_menu(MULTI_ON_OPTION)["question"] == "Pick any")
check("question: a multi-question form takes the question, not the tab bar above it", srv.parse_menu(WIZARD_ON_NEXT)["question"].startswith("Capture entry points: how should you get things INTO the app?"))
check("question: the review screen's question", srv.parse_menu(REVIEW)["question"] == "Ready to submit your answers?")
mf = srv.parse_menu(FREE)
check("a 'Type something.' row is recognised as a text row, and no other row is", [o["free"] for o in mf["options"]] == [False, False, True, False] and mf["question"] == "Pick one")
check("question: no menu means no question", srv.parse_menu("just some prose\nno options here") is None)


# A form with several questions draws a tab bar above each step, and Esc on any step cancels the WHOLE form (Shepr's Skip and send warns about it).
check("form: a step of a multi-question form is recognised by its tab bar", srv.parse_menu(WIZARD_ON_NEXT)["form"] is True)
check("form: the form's review step is recognised too", srv.parse_menu(REVIEW)["form"] is True)
check("form: a plain permission dialog is not a form", srv.parse_menu(SINGLE)["form"] is False)
check("form: a single multi-select question with no tab bar is not a form", srv.parse_menu(MULTI_ON_OPTION)["form"] is False)
check("form: a single question with a typing row is not a form", srv.parse_menu(FREE)["form"] is False)

sys.exit(0 if ok else 1)
