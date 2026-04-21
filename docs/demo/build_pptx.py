"""Generate the NHTSA Defect Intel demo deck as a native .pptx.

Run:  python docs/demo/build_pptx.py
Output: docs/demo/nhtsa_defect_intel_demo.pptx

Slides:
  1. What this agent does.
  2. Architecture (drawn as native PPT shapes — no image dependency).
  3. Dataset + Agent logic + What I did differently.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Emu, Inches, Pt

OUT = Path(__file__).with_name("nhtsa_defect_intel_demo.pptx")

# ---------- palette ----------
C_TITLE = RGBColor(0x1A, 0x23, 0x7E)       # deep indigo
C_BODY = RGBColor(0x21, 0x21, 0x21)
C_MUTED = RGBColor(0x55, 0x55, 0x55)
C_ACCENT = RGBColor(0xC6, 0x28, 0x28)      # accent red for callouts
C_DATA = RGBColor(0xFF, 0xF8, 0xE1)        # pale yellow for data layer
C_DATA_BRD = RGBColor(0xF9, 0xA8, 0x25)
C_TOOL = RGBColor(0xFC, 0xE4, 0xEC)        # pink for tools
C_TOOL_BRD = RGBColor(0xC2, 0x18, 0x5B)
C_SVC = RGBColor(0xED, 0xE7, 0xF6)         # lavender for services/agent
C_SVC_BRD = RGBColor(0x45, 0x27, 0xA0)
C_OBS = RGBColor(0xE0, 0xF2, 0xF1)         # teal for observability
C_OBS_BRD = RGBColor(0x00, 0x69, 0x5C)
C_USER = RGBColor(0xE8, 0xF5, 0xE9)
C_USER_BRD = RGBColor(0x2E, 0x7D, 0x32)
C_MEM = RGBColor(0xFF, 0xF3, 0xE0)
C_MEM_BRD = RGBColor(0xE6, 0x51, 0x00)

# ---------- geometry ----------
SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)


def _set_text(tf, text, *, size=18, bold=False, color=C_BODY, italic=False, align=None):
    tf.word_wrap = True
    tf.margin_left = Emu(0)
    tf.margin_right = Emu(0)
    tf.margin_top = Emu(0)
    tf.margin_bottom = Emu(0)
    p = tf.paragraphs[0]
    if align is not None:
        p.alignment = align
    run = p.runs[0] if p.runs else p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color


def _add_bullets(tf, items, *, size=14, color=C_BODY, bold_first=False):
    tf.word_wrap = True
    tf.margin_left = Emu(0)
    tf.margin_right = Emu(0)
    tf.margin_top = Emu(0)
    tf.margin_bottom = Emu(0)
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.level = 0
        p.space_after = Pt(3)
        run = p.add_run()
        run.text = "•  " + item
        run.font.size = Pt(size)
        run.font.color.rgb = color
        if bold_first and i == 0:
            run.font.bold = True


def _box(slide, x, y, w, h, *, fill, border, text=None, size=12, bold=False, color=C_BODY, rounded=True):
    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
    shp = slide.shapes.add_shape(shape_type, x, y, w, h)
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    shp.line.color.rgb = border
    shp.line.width = Pt(1.25)
    shp.shadow.inherit = False
    if text is not None:
        tf = shp.text_frame
        tf.word_wrap = True
        tf.margin_left = Emu(36000)
        tf.margin_right = Emu(36000)
        tf.margin_top = Emu(18000)
        tf.margin_bottom = Emu(18000)
        p = tf.paragraphs[0]
        from pptx.enum.text import PP_ALIGN
        p.alignment = PP_ALIGN.CENTER
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if i == 0:
                para = p
            else:
                para = tf.add_paragraph()
                para.alignment = PP_ALIGN.CENTER
            run = para.add_run()
            run.text = line
            run.font.size = Pt(size if i == 0 else max(9, size - 2))
            run.font.bold = bold if i == 0 else False
            run.font.color.rgb = color
    return shp


def _arrow(slide, x1, y1, x2, y2, *, color=C_MUTED, weight=1.75, dashed=False):
    line = slide.shapes.add_connector(1, x1, y1, x2, y2)  # STRAIGHT
    line.line.color.rgb = color
    line.line.width = Pt(weight)
    if dashed:
        from pptx.enum.dml import MSO_LINE_DASH_STYLE
        line.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    # arrowhead
    from pptx.oxml.ns import qn
    from lxml import etree

    ln = line.line._get_or_add_ln()
    tail = etree.SubElement(ln, qn("a:tailEnd"))
    tail.set("type", "triangle")
    tail.set("w", "med")
    tail.set("h", "med")
    return line


# ---------- build ----------
def build():
    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H

    blank = prs.slide_layouts[6]

    _slide1_what(prs.slides.add_slide(blank))
    _slide2_architecture(prs.slides.add_slide(blank))
    _slide3_three_questions(prs.slides.add_slide(blank))

    prs.save(OUT)
    print(f"wrote {OUT}")


# ---------- slide 1 ----------
def _slide1_what(slide):
    # Title
    title = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(12.3), Inches(0.9))
    _set_text(title.text_frame, "NHTSA Defect Intelligence Agent", size=36, bold=True, color=C_TITLE)

    sub = slide.shapes.add_textbox(Inches(0.5), Inches(1.15), Inches(12.3), Inches(0.5))
    _set_text(
        sub.text_frame,
        "An agentic assistant over 10M+ U.S. vehicle-defect records — recalls, complaints, investigations, SGO AV crashes, TSBs",
        size=16, italic=True, color=C_MUTED,
    )

    # left column — one-liner + personas
    left_title = slide.shapes.add_textbox(Inches(0.5), Inches(1.95), Inches(6.0), Inches(0.4))
    _set_text(left_title.text_frame, "What it does", size=20, bold=True, color=C_TITLE)

    left = slide.shapes.add_textbox(Inches(0.5), Inches(2.35), Inches(6.0), Inches(1.5))
    _set_text(
        left.text_frame,
        "One chat surface that joins structured NHTSA fact tables with the free-text narratives behind them, so analysts can ask cross-corpus questions in English and get cited, traceable answers.",
        size=13, color=C_BODY,
    )

    p_title = slide.shapes.add_textbox(Inches(0.5), Inches(3.95), Inches(6.0), Inches(0.4))
    _set_text(p_title.text_frame, "Who uses it", size=18, bold=True, color=C_TITLE)

    p_box = slide.shapes.add_textbox(Inches(0.5), Inches(4.35), Inches(6.0), Inches(2.8))
    _add_bullets(
        p_box.text_frame,
        [
            "OEM quality teams — how do our recall patterns compare to peers?",
            "Regulatory analysts — what themes are emerging in 2024 ADAS complaints?",
            "Safety journalists / researchers — what did NHTSA do about phantom braking?",
            "Reliability engineers — component-level trend spotting across OEMs.",
        ],
        size=13,
    )

    # right column — example multi-turn interaction + takeaways
    ex_title = slide.shapes.add_textbox(Inches(6.8), Inches(1.95), Inches(6.1), Inches(0.4))
    _set_text(ex_title.text_frame, "Example — multi-turn, cited", size=20, bold=True, color=C_TITLE)

    ex_box = _box(
        slide, Inches(6.8), Inches(2.35), Inches(6.1), Inches(1.9),
        fill=RGBColor(0xF5, 0xF5, 0xF5), border=C_MUTED, text=None, rounded=True,
    )
    tf = ex_box.text_frame
    tf.word_wrap = True
    tf.margin_left = Emu(90000)
    tf.margin_right = Emu(90000)
    tf.margin_top = Emu(54000)
    tf.margin_bottom = Emu(54000)
    lines = [
        ("User:", True, C_TITLE),
        ("  Describe common complaint patterns for ADAS lane-keep assist nuisance activations across OEMs.", False, C_BODY),
        ("Agent:", True, C_TITLE),
        ("  [3 patterns across Honda / Toyota / Subaru / GM, with 6 ODI IDs cited]", False, C_BODY),
        ("User:", True, C_TITLE),
        ("  Narrow that to Honda and Toyota only.", False, C_BODY),
        ("Agent:", True, C_TITLE),
        ("  [same frame, only the two OEMs — session memory held the prior context]", False, C_BODY),
    ]
    for i, (text, bold, color) in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(1)
        run = p.add_run()
        run.text = text
        run.font.size = Pt(11)
        run.font.bold = bold
        run.font.color.rgb = color

    tk_title = slide.shapes.add_textbox(Inches(6.8), Inches(4.4), Inches(6.1), Inches(0.4))
    _set_text(tk_title.text_frame, "Four things to remember", size=18, bold=True, color=C_TITLE)

    tk_box = slide.shapes.add_textbox(Inches(6.8), Inches(4.8), Inches(6.1), Inches(2.4))
    _add_bullets(
        tk_box.text_frame,
        [
            "Cross-corpus — a single question can hit SQL and semantic search in parallel.",
            "Always cited — every claim carries a campaign / ODI / TSB ID inline.",
            "Stateful — Lakebase Postgres holds session + accumulated filters across turns.",
            "Production-graded — Mosaic AI serving, MLflow auto-trace, hourly LLM-judge scoring, live dashboard.",
        ],
        size=13,
    )


# ---------- slide 2 — architecture drawn natively ----------
def _slide2_architecture(slide):
    # Title
    t = slide.shapes.add_textbox(Inches(0.4), Inches(0.2), Inches(12.5), Inches(0.6))
    _set_text(t.text_frame, "Architecture — offline pipeline · runtime query flow", size=26, bold=True, color=C_TITLE)

    # ============================================================
    # PANEL A — offline pipeline (top strip)
    # ============================================================
    panel_a_y = Inches(0.95)
    strip_h = Inches(0.9)
    label_h = Inches(0.3)

    label_a = slide.shapes.add_textbox(Inches(0.4), panel_a_y, Inches(12.5), label_h)
    _set_text(label_a.text_frame, "A · Offline data pipeline (runs on schedule)", size=13, bold=True, color=C_MUTED)

    # 4 boxes LR: Sources → Bronze → Silver → Gold
    y = Inches(1.3)
    box_w = Inches(2.5)
    box_h = Inches(0.95)
    gap = Inches(0.22)
    x = Inches(0.4)

    a_boxes = [
        ("NHTSA open data", "recalls · complaints · investigations\nSGO AV crashes · TSB PDFs"),
        ("Bronze", "raw append-only\nPDFs in UC Volume"),
        ("Silver", "typed · deduped · PII-scrubbed\nai_parse_document on PDFs"),
        ("Gold", "star schema + narrative chunks\n3 fact · 4 dim · 1 chunk table"),
    ]
    a_shapes = []
    for title, body in a_boxes:
        text = f"{title}\n{body}"
        shp = _box(slide, x, y, box_w, box_h, fill=C_DATA, border=C_DATA_BRD, text=text, size=12, bold=True)
        a_shapes.append(shp)
        x += box_w + gap

    # arrows between a_shapes
    for i in range(len(a_shapes) - 1):
        s1 = a_shapes[i]
        s2 = a_shapes[i + 1]
        x1 = s1.left + s1.width
        xm = s2.left
        ym = y + box_h // 2
        _arrow(slide, x1, ym, xm, ym, weight=2)

    # Retrieval surfaces row — 4 tools, horizontal
    y2 = Inches(2.45)
    label_b = slide.shapes.add_textbox(Inches(0.4), y2, Inches(12.5), label_h)
    _set_text(label_b.text_frame, "Retrieval surfaces — exactly 4 tools (narrow by design)", size=13, bold=True, color=C_MUTED)

    y3 = Inches(2.8)
    tool_w = Inches(3.0)
    tool_h = Inches(0.85)
    tool_gap = Inches(0.12)
    tx = Inches(0.4)
    tools = [
        ("🛠  genie_recalls", "Genie over gold fact tables\ncounts · trends · rankings"),
        ("🛠  vector_search_narrative", "VS index on gold_narrative_chunks\ncomplaints · TSBs · investigations · SGO"),
        ("🛠  fetch_tsb", "UC function · 1 TSB by item number"),
        ("🛠  fetch_investigation", "UC function · 1 case file by action number"),
    ]
    tool_shapes = []
    for head, body in tools:
        text = f"{head}\n{body}"
        shp = _box(slide, tx, y3, tool_w, tool_h, fill=C_TOOL, border=C_TOOL_BRD, text=text, size=11, bold=True)
        tool_shapes.append(shp)
        tx += tool_w + tool_gap

    # dashed arrows from Gold/Silver to tool row (just 2 representative ones)
    gold_shape = a_shapes[3]
    gx = gold_shape.left + gold_shape.width // 2
    gy = gold_shape.top + gold_shape.height
    _arrow(slide, gx, gy, tool_shapes[0].left + tool_shapes[0].width // 2, y3, dashed=True)
    _arrow(slide, gx, gy, tool_shapes[1].left + tool_shapes[1].width // 2, y3, dashed=True)
    silver_shape = a_shapes[2]
    sx = silver_shape.left + silver_shape.width // 2
    sy = silver_shape.top + silver_shape.height
    _arrow(slide, sx, sy, tool_shapes[2].left + tool_shapes[2].width // 2, y3, dashed=True)
    _arrow(slide, sx, sy, tool_shapes[3].left + tool_shapes[3].width // 2, y3, dashed=True)

    # ============================================================
    # PANEL B — runtime query flow (bottom half)
    # ============================================================
    panel_b_y = Inches(3.95)
    label_c = slide.shapes.add_textbox(Inches(0.4), panel_b_y, Inches(12.5), label_h)
    _set_text(label_c.text_frame, "B · Runtime query flow (every chat turn)", size=13, bold=True, color=C_MUTED)

    # Row 1: User → Review App → Endpoint → Agent (all connected LR)
    ry = Inches(4.35)
    rh = Inches(0.8)
    rw = Inches(2.4)
    rgap = Inches(0.25)
    rx = Inches(0.4)

    user = _box(slide, rx, ry, rw, rh, fill=C_USER, border=C_USER_BRD, text="👤  Analyst /\nregulator", size=12, bold=True)
    rx += rw + rgap
    review = _box(slide, rx, ry, rw, rh, fill=C_SVC, border=C_SVC_BRD, text="Review App\nchat UI", size=12, bold=True)
    rx += rw + rgap
    endpoint = _box(slide, rx, ry, rw, rh, fill=C_SVC, border=C_SVC_BRD,
                    text="Mosaic AI endpoint\nnhtsa-agent-endpoint-dev-pg · v5", size=11, bold=True)
    rx += rw + rgap
    agent = _box(slide, rx, ry, rw, rh, fill=C_SVC, border=C_SVC_BRD,
                 text="NhtsaResponsesAgent\nLlama-4-Maverick · temp 0.2", size=11, bold=True)

    # LR arrows between them
    for s1, s2 in [(user, review), (review, endpoint), (endpoint, agent)]:
        _arrow(slide, s1.left + s1.width, s1.top + s1.height // 2,
               s2.left, s2.top + s2.height // 2, weight=2)

    # Row 2: Agent dispatches parallel tool calls → dashed vertical arrows down to the 4 tool boxes (reuse the ones from Panel A's tool row)
    # Draw one down-arrow from agent to a midpoint near the bottom centre of tool row, then branch
    agent_bottom_x = agent.left + agent.width // 2
    agent_bottom_y = agent.top + agent.height
    # Downward arrow from agent to a band just above tool row (but tool row is at y3 ~ 2.8" which is ABOVE runtime panel — so visually they're above).
    # To show "dispatch" without crossing too many wires, we draw a curved arrow label instead.

    # Simpler: draw an arrow from Agent UP to the tool row via a vertical line on the RIGHT side of the slide
    up_x = agent.left + agent.width - Inches(0.2)
    up_y_bot = agent.top
    up_y_top = y3 + tool_h
    _arrow(slide, up_x, up_y_bot, up_x, up_y_top + Inches(0.05), weight=1.75, dashed=False)

    # Label for dispatch arrow
    lbl_disp = slide.shapes.add_textbox(up_x - Inches(1.5), agent.top - Inches(0.32), Inches(1.4), Inches(0.3))
    _set_text(lbl_disp.text_frame, "dispatch → parallel tool calls", size=10, italic=True, color=C_ACCENT)

    # Row 3: Agent ↔ Lakebase memory (left of agent, below)
    my = ry + rh + Inches(0.35)
    mem = _box(
        slide, Inches(4.3), my, Inches(4.7), Inches(0.8),
        fill=C_MEM, border=C_MEM_BRD,
        text="🗂  Lakebase Postgres  ·  agent_sessions + agent_turns  ·  accumulated_filters JSONB",
        size=11, bold=True,
    )
    # bi-directional arrow agent ↔ memory (draw two arrows)
    agent_cx = agent.left + agent.width // 2
    agent_by = agent.top + agent.height
    mem_cx = mem.left + mem.width // 2
    _arrow(slide, agent_cx - Inches(0.15), agent_by, mem_cx - Inches(0.15), mem.top, weight=1.5)
    _arrow(slide, mem_cx + Inches(0.15), mem.top, agent_cx + Inches(0.15), agent_by, weight=1.5)
    lbl_mem = slide.shapes.add_textbox(mem.left - Inches(3.3), my + Inches(0.1), Inches(3.2), Inches(0.3))
    _set_text(lbl_mem.text_frame, "read prior context ↔ write turn", size=10, italic=True, color=C_MEM_BRD)

    # Row 4: Observability — trace → evaluate → dashboard (right of memory)
    oy = my + Inches(1.0)
    ow = Inches(3.0)
    oh = Inches(0.7)
    ogap = Inches(0.18)
    ox = Inches(0.4)
    obs_labels = [
        ("MLflow traces", "trace_logs_<exp_id>"),
        ("Hourly evaluate job", "3 cheap + 3 Guidelines scorers"),
        ("Aggregated view", "nhtsa_traces_aggregated_pg"),
        ("📊 Monitoring Dashboard", "latency · tool mix · judge pass rate"),
    ]
    # Fit 4 obs boxes in a row within slide width
    # Recompute widths
    total_w = SLIDE_W - Inches(0.8)
    ow = (total_w - ogap * 3) / 4
    obs_shapes = []
    for head, body in obs_labels:
        text = f"{head}\n{body}"
        shp = _box(slide, ox, oy, ow, oh, fill=C_OBS, border=C_OBS_BRD, text=text, size=11, bold=True)
        obs_shapes.append(shp)
        ox += ow + ogap

    for i in range(len(obs_shapes) - 1):
        s1 = obs_shapes[i]
        s2 = obs_shapes[i + 1]
        _arrow(slide, s1.left + s1.width, s1.top + s1.height // 2,
               s2.left, s2.top + s2.height // 2, weight=2)

    # dashed arrow: endpoint → MLflow traces (auto-trace)
    _arrow(slide, endpoint.left + endpoint.width // 2, endpoint.top + endpoint.height,
           obs_shapes[0].left + obs_shapes[0].width // 2, obs_shapes[0].top, dashed=True, weight=1.5)
    lbl_trace = slide.shapes.add_textbox(endpoint.left, endpoint.top + endpoint.height + Inches(0.02), Inches(2.5), Inches(0.3))
    _set_text(lbl_trace.text_frame, "auto-trace per turn", size=10, italic=True, color=C_OBS_BRD)


# ---------- slide 3 ----------
def _slide3_three_questions(slide):
    t = slide.shapes.add_textbox(Inches(0.4), Inches(0.2), Inches(12.5), Inches(0.6))
    _set_text(t.text_frame, "Dataset  ·  Agent logic  ·  What I did differently", size=26, bold=True, color=C_TITLE)

    # 3 columns
    col_w = Inches(4.15)
    col_gap = Inches(0.15)
    col_y = Inches(0.95)
    col_h = Inches(6.4)

    cols = [
        {
            "header": "Dataset & use case",
            "accent": C_DATA_BRD,
            "fill": RGBColor(0xFF, 0xFD, 0xE7),
            "items": [
                ("Domain", "U.S. vehicle-defect intelligence — the only public, authoritative source for recalls, consumer complaints, open investigations, SGO autonomous-vehicle crash reports, and manufacturer TSBs."),
                ("Why an agent", "Every question is a mix of quantitative (how many), qualitative (what kinds), and document-level (what did the agency do). Single-tool RAG or text-to-SQL alone cannot answer these."),
                ("Scale", "~10M rows in Silver; 350K narrative chunks in the vector index; 5 disparate feeds with different schemas and cadences."),
                ("Data challenges", "PDFs for TSBs + investigations (ai_parse_document); TSBs split into 5-year ZIPs after 2024 rewrite; SGO direct-URL workaround; PII scrubbing in narratives; component-code + make/model normalisation via an in-repo taxonomy."),
                ("Citable IDs", "Campaign #, ODI #, PE/EA #, TSB # — every answer must carry them inline. Gives a crisp, testable evaluation target."),
            ],
        },
        {
            "header": "Agent logic",
            "accent": C_SVC_BRD,
            "fill": RGBColor(0xF3, 0xEF, 0xFB),
            "items": [
                ("Four tools, narrow on purpose", "genie_recalls (Genie over star schema) · vector_search_narrative (VS over narrative chunks, with metadata filters) · fetch_tsb · fetch_investigation (UC functions)."),
                ("Routing", "System prompt enumerates intent → tool mapping explicitly. 'How many / top N / trends' → Genie. 'What kinds / themes / examples' → Vector Search. Specific ID → fetch_*."),
                ("Agent surface", "mlflow.pyfunc.ResponsesAgent subclass. MLflow auto-traces every turn + tool + LLM span. Parallel tool calls enabled for independent sub-questions."),
                ("Memory", "Lakebase Postgres (Projects API) keyed by UUID session_id. Each turn merges refinements into an accumulated_filters JSONB and passes them into tool calls."),
                ("Live demo (3 queries)", "Q2: phantom-braking — parallel Genie + VS.  Q4: lane-keep across OEMs — pure synthesis.  Q5: 'narrow to Honda and Toyota' — session-memory refinement."),
            ],
        },
        {
            "header": "What I did differently",
            "accent": C_ACCENT,
            "fill": RGBColor(0xFD, 0xEC, 0xEA),
            "items": [
                ("Data & tuning", "Chunk 800/overlap 100 (TSBs are procedural); temp 0.2 not 0.7 (defect answers must be factual); top-K 8 not 5 (themes need more evidence)."),
                ("Three-tier eval", "Tier-1 deterministic scalar match (25 Qs) · Tier-2 citation-grounded (25 Qs, agent must surface a specific ID) · Tier-3 synthesis 5-point rubric (25 Qs, pass at 3.5)."),
                ("Scorer split", "3 cheap heuristics on every trace (cite_id_present, mentions_oem, word_count_under) + 3 Guidelines LLM judges (factual_defect, cite_every_claim, stays_in_scope) on a 10% sample. Cheap catches regressions; judges catch style drift."),
                ("Infrastructure", "Lakebase via the new Projects/PostgresAPI — no MLflow auto-grant, so we reuse dev_SPN + pg_api.create_role + env-var injection at deploy time. project_config.yml shipped inside the model artifact via code_paths with a __file__-based fallback resolver."),
                ("Top 3 challenges solved", "(1) Serving-pod Lakebase auth via dev_SPN + {{secrets/…}} env vars.  (2) trace_logs.response is VARIANT, not STRING — CAST first.  (3) Cheap scorers store true/false, Guidelines judges store JSON-quoted \"yes\"/\"no\" — view CASE must match '\"yes\"', not 'Pass'."),
            ],
        },
    ]

    x = Inches(0.4)
    for col in cols:
        # column background
        _box(slide, x, col_y, col_w, col_h, fill=col["fill"], border=col["accent"], text=None, rounded=True)

        # header (inside column, at top)
        hdr = slide.shapes.add_textbox(x + Inches(0.15), col_y + Inches(0.1), col_w - Inches(0.3), Inches(0.45))
        _set_text(hdr.text_frame, col["header"], size=18, bold=True, color=col["accent"])

        # items as sub-sections: label (bold) + body
        body_top = col_y + Inches(0.65)
        body_box = slide.shapes.add_textbox(x + Inches(0.2), body_top, col_w - Inches(0.4), col_h - Inches(0.8))
        tf = body_box.text_frame
        tf.word_wrap = True
        tf.margin_left = Emu(0)
        tf.margin_right = Emu(0)
        tf.margin_top = Emu(0)
        tf.margin_bottom = Emu(0)
        for i, (label, body) in enumerate(col["items"]):
            # label para
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.space_after = Pt(1)
            p.space_before = Pt(4) if i > 0 else Pt(0)
            run = p.add_run()
            run.text = label
            run.font.size = Pt(11)
            run.font.bold = True
            run.font.color.rgb = col["accent"]
            # body para
            p2 = tf.add_paragraph()
            p2.space_after = Pt(2)
            run2 = p2.add_run()
            run2.text = body
            run2.font.size = Pt(10)
            run2.font.color.rgb = C_BODY

        x += col_w + col_gap


if __name__ == "__main__":
    build()
