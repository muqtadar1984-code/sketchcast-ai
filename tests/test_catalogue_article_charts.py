"""Engine charts attached to a catalogue article's worked examples (charts
phase 6, founder 2026-10-09). The texts are the catalogue's own —
'Simultaneous Linear Equations', 'Linear Inequalities…', 'Quadratic
Expressions…', 'Coordinates and Plotting Linear Graphs' (2026-10-09)."""

from __future__ import annotations

from catalogue.article_charts import attach_charts, derive_example, graph_note
from catalogue.figures import load_figures
from maths.charts import chart_for
from tests.catalogue_fakes import FakeSB

SYSTEM = ("Solve the following pair of simultaneous linear equations using substitution:\nEquation 1: y = 2x + 1\n"
          "Equation 2: 3x + y = 11",
          "Step 1: Equation 1 already has y isolated with the expression 2x + 1.\nStep 2: Substitute (2x + 1) in place of y "
          "in Equation 2:\n3x + (2x + 1) = 11\nStep 3: Simplify and solve for x:\n5x + 1 = 11\n5x = 10\nx = 2\nStep 4: "
          "Substitute x = 2 back into Equation 1 to find y:\ny = 2(2) + 1\ny = 4 + 1\ny = 5\nStep 5: Check the solution in "
          "Equation 2:\n3(2) + 5 = 6 + 5 = 11, which is correct.\nSolution: x = 2, y = 5, or as a coordinate point (2, 5).")
ELIMINATION = ("Solve the following pair of simultaneous linear equations using elimination:\nEquation 1: 2x + 3y = 13\n"
               "Equation 2: 4x - y = 5",
               "Step 1: Choose to eliminate y.\nStep 4: Solve for x:\nx = 2\nStep 5: Substitute x = 2 into the original "
               "Equation 2 to find y:\n4(2) - y = 5\n8 - y = 5\n-y = -3\ny = 3\nStep 6: Check in Equation 1:\n"
               "2(2) + 3(3) = 4 + 9 = 13, which is correct.\nSolution: x = 2, y = 3.")
WORDS = ("A shop sells notebooks and pens. John buys 2 notebooks and 3 pens for 8 pounds. Sarah buys 1 notebook and 4 pens "
         "for 7 pounds. Find the cost of one notebook and one pen.",
         "Step 1: Let n be the cost of one notebook, p the cost of one pen.\nEquation 1: 2n + 3p = 8\nEquation 2: n + 4p = 7\n"
         "p = 1.2\nn = 2.2\nSolution: One notebook costs 2.20 pounds and one pen costs 1.20 pounds.")
INEQ = ("Solve and graph the inequality 5x + 3 >= 23.",
        "Step 1: Write down the original inequality: 5x + 3 >= 23.\nStep 2: Subtract 3 from both sides: 5x >= 20.\n"
        "Step 3: Divide both sides by the positive coefficient 5: x >= 20 / 5.\nStep 4: Calculate the final inequality: "
        "x >= 4.\nStep 5: Represent the solution on a number line by placing a closed circle at 4 and drawing a bold arrow "
        "extending to the right.")
INEQ_FLIP = ("Solve and graph the inequality 7 - 3x < 16.",
             "Step 1: 7 - 3x < 16.\nStep 2: -3x < 9.\nStep 3: Divide by -3 and reverse the sign.\nStep 4: Calculate the final "
             "inequality: x > -3.\nStep 5: Represent the solution on a number line by placing an open circle at -3 and "
             "drawing a bold arrow extending to the right.")
QUAD_SOLVE = ("Solve the quadratic equation x^2 - 7x + 12 = 0 by factorising.",
              "1. Check the standard form: x^2 - 7x + 12 = 0.\n2. Find two numbers that multiply to 12 and add to -7: -3 "
              "and -4.\n3. Write the factorised equation: (x - 3)(x - 4) = 0.\n4. Set each factor to zero: x - 3 = 0 or "
              "x - 4 = 0.\n5. Solve each linear equation for x to find the solutions: x = 3 and x = 4.")
FACTORISE = ("Factorise the quadratic trinomial x^2 + 9x + 18 completely.",
             "1. Identify b = 9 and c = 18.\n2. Look for two integers that multiply to 18 and add to 9.\n5. Select 3 and 6.\n"
             "6. Write the factorised expression using these two numbers: (x + 3)(x + 6).")
PLOT = ("Construct a table of values and plot the linear graph for the equation y = 2x - 3 using x-values from -1 to 3.",
        "Step 1: Set up a table of values.\nStep 9: Draw a straight line through all points using a ruler and label the "
        "line with the equation y = 2x - 3.")
MEAN = ("Find the mean of the numbers 4, 8, 6, 10, and 12.",
        "Step 1: Add the numbers: 4 + 8 + 6 + 10 + 12 = 40.\nStep 2: Divide by how many there are: 40 / 5 = 8.\n"
        "The mean is 8.")


def test_the_catalogue_texts_read_back_into_plottable_examples():
    ex = derive_example("w1", *SYSTEM)
    assert ex and ex.task == "solve_system" and ex.givens == ["y = 2x + 1", "3x + y = 11"] and ex.final_answer == ["x = 2", "y = 5"]
    assert chart_for(ex)["point"] == "(2, 5)"
    ex = derive_example("w2", *ELIMINATION)
    assert ex and ex.givens == ["2x + 3y = 13", "4x - y = 5"] and chart_for(ex)["point"] == "(2, 3)"
    assert derive_example("w3", *WORDS) is None                      # n and p: not a chart in x and y
    ex = derive_example("w1", *INEQ)
    assert ex and ex.task == "solve_inequality" and ex.givens == ["5x + 3 >= 23"] and ex.final_answer == ["x >= 4"]
    assert chart_for(ex)["shape"] == "right"
    ex = derive_example("w2", *INEQ_FLIP)
    assert ex and ex.final_answer == ["x > -3"] and chart_for(ex)["closed"] == [False, False]
    ex = derive_example("w3", *QUAD_SOLVE)
    assert ex and ex.task == "solve" and ex.givens == ["x^2 - 7x + 12 = 0"] and ex.final_answer == ["x = 3 or x = 4"]
    assert chart_for(ex)["roots"] == ["3", "4"]
    ex = derive_example("w1", *FACTORISE)
    assert ex and ex.task == "factorise" and ex.givens == ["x^2 + 9x + 18"] and ex.final_answer == ["(x + 3)(x + 6)"]
    assert chart_for(ex)["roots"] == ["-6", "-3"]
    ex = derive_example("w2", *PLOT)
    assert ex and ex.task == "solve" and ex.givens == ["y = 2x - 3"] and chart_for(ex)["point"] is None
    ex = derive_example("w1", *MEAN)
    assert ex and ex.task == "mean" and ex.givens == ["4, 8, 6, 10, 12"] and ex.final_answer == ["8"]
    assert chart_for(ex)["value"] == "8"
    # a wrong stated answer is read faithfully and then refused by the engine
    wrong = derive_example("w1", SYSTEM[0], SYSTEM[1].replace("Solution: x = 2, y = 5", "Solution: x = 3, y = 5"))
    assert wrong.final_answer == ["x = 3", "y = 5"] and chart_for(wrong) is None
    assert graph_note(chart_for(derive_example("w1", *SYSTEM))) == "Graph: the lines y = 2x + 1 and 3x + y = 11 cross at (2, 5) — the solution."


def _article(sb: FakeSB) -> dict:
    sb.tables["topics"].append({"id": "t-sim", "title": "Simultaneous Linear Equations", "subject": "Mathematics"})
    art = {"id": "art-sim", "topic_id": "t-sim", "title": "Simultaneous Linear Equations", "language": "en", "version": 1,
           "status": "approved", "depth_node_id": None,
           "sections": [{"id": "s1", "heading": "Introduction", "body_md": "…", "figure_keys": ["intersection_line"], "covers": []},
                        {"id": "s3", "heading": "Solving by Substitution", "body_md": "…", "figure_keys": [], "covers": []}],
           "worked_examples": [{"id": "w1", "problem": SYSTEM[0], "solution_md": SYSTEM[1]},
                               {"id": "w2", "problem": ELIMINATION[0], "solution_md": ELIMINATION[1]},
                               {"id": "w3", "problem": WORDS[0], "solution_md": WORDS[1]}]}
    sb.tables["topic_articles"].append(art)
    sb.tables["article_figures"].append({"id": "f-1", "article_id": "art-sim", "figure_key": "intersection_line",
                                         "caption": "two lines", "spec": {"subject": "two lines crossing", "parts": ["line"]},
                                         "status": "draft", "sort": 0})
    return art


def test_charts_are_attached_to_the_article_once_and_the_figure_job_leaves_them_alone():
    sb = FakeSB()
    art = _article(sb)
    s = attach_charts(sb, art)
    assert (s["examined"], s["charted"], s["attached"]) == (3, 2, 2) and s["skipped"][0][0] == "w3"
    figs = {f["figure_key"]: f for f in sb.tables["article_figures"]}
    assert set(figs) == {"intersection_line", "chart_w1", "chart_w2"}
    assert figs["chart_w1"]["status"] == "rendered" and figs["chart_w1"]["spec"]["engine"] is True
    assets = sb.tables["visual_assets"]
    assert len(assets) == 2 and all(a["status"] == "candidate" and a["provenance"] == "engine" for a in assets)
    assert figs["chart_w1"]["visual_asset_id"] == assets[0]["id"]
    uploads = [k for k in sb.files if k[0] == "visual-assets"]
    assert len(uploads) == 2 and all(p.startswith("engine/charts/art-sim/") and p.endswith(".png") for _b, p in uploads)
    assert sb.files[uploads[0]][:4] == b"\x89PNG"
    # the keys went on the last section (no 'Worked examples' section here) and the note under each solution
    saved = sb.tables["topic_articles"][0]
    assert saved["sections"][1]["figure_keys"] == ["chart_w1", "chart_w2"] and saved["sections"][0]["figure_keys"] == ["intersection_line"]
    assert saved["worked_examples"][0]["solution_md"].endswith("\n\nGraph: the lines y = 2x + 1 and 3x + y = 11 cross at (2, 5) — the solution.")
    assert saved["worked_examples"][2]["solution_md"] == WORDS[1]
    # a second run attaches nothing new and writes nothing
    before = len(sb.log)
    s2 = attach_charts(sb, saved)
    assert s2["attached"] == 0 and len(sb.log) == before
    # the image-model figure job sees only its own draft figure, never the engine's
    assert [f["figure_key"] for f in load_figures(sb, "art-sim", force=False)] == ["intersection_line"]
    assert [f["figure_key"] for f in load_figures(sb, "art-sim", force=True)] == ["intersection_line"]
    # a dry run touches nothing
    sb2 = FakeSB()
    art2 = _article(sb2)
    assert attach_charts(sb2, art2, dry_run=True)["attached"] == 2 and not sb2.log and not sb2.files


def test_a_translated_article_gets_the_figure_but_no_english_note():
    sb = FakeSB()
    art = _article(sb)
    art["language"] = "ms"
    s = attach_charts(sb, art)
    assert s["attached"] == 2
    saved = sb.tables["topic_articles"][0]
    assert saved["worked_examples"][0]["solution_md"] == SYSTEM[1]
    assert saved["sections"][1]["figure_keys"] == ["chart_w1", "chart_w2"]
