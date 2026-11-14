"""Chạy bộ đánh giá agent và xuất báo cáo.

    .venv/bin/python evals/run_evals.py                       # live: Ollama + NeMo thật
    .venv/bin/python evals/run_evals.py --offline             # không model (nhanh, cho CI)
    .venv/bin/python evals/run_evals.py --category catalog --repeat 3
    .venv/bin/python evals/run_evals.py --judge               # thêm LLM-as-judge (model nặng)
    .venv/bin/python evals/run_evals.py --save-baseline       # lưu kết quả làm mốc
    .venv/bin/python evals/run_evals.py --fail-on-regression  # exit 1 nếu case từng đạt nay trượt

Kết quả: evals/results/<thời điểm>.json và .md
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import statistics
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EVALS = ROOT / "evals"
MODEL_CATEGORIES = {"catalog", "general"}


def load_env():
    env = {}
    path = ROOT / "local.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    return env


def needs_model(case):
    if "specs" in case["tags"]:
        return False  # tra thông số chạy bằng luật, không cần model
    return case["category"] in MODEL_CATEGORIES or "subtle" in case["tags"] or case["category"] == "robustness"


def percentile(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


JUDGE_PROMPT = """Bạn là giám khảo chấm chatbot CSKH của cửa hàng điện tử. Chấm câu trả lời theo thang 1-5:
- relevance: trả lời đúng điều khách hỏi
- faithfulness: mọi thông tin đều có trong NGUỒN (5 = không bịa gì; câu từ chối/chuyển nhân viên hợp lý = 5)
- helpfulness: khách có thể hành động tiếp ngay (rõ ràng, đủ ý, có bước tiếp theo)
- tone: lịch sự, tự nhiên, đúng văn phong CSKH tiếng Việt
Chỉ trả JSON: {"relevance":n,"faithfulness":n,"helpfulness":n,"tone":n,"comment":"<=25 từ"}"""


def judge(model, question, result, endpoint="http://127.0.0.1:11434/api/chat"):
    sources = "\n".join(f"- {c.get('topic')}: {(c.get('text') or '')[:300]}" for c in result.get("citations", [])[:6])
    shown = (result.get("answer") or "") + ("\n" + sources if result.get("status") == "evidence" else "")
    body = {"model": model, "stream": False, "format": "json", "think": False, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": JUDGE_PROMPT},
                         {"role": "user", "content": f"CÂU HỎI: {question}\n\nNGUỒN:\n{sources or '(không có)'}\n\n"
                                                     f"CÂU TRẢ LỜI HIỂN THỊ CHO KHÁCH:\n{shown}"}]}
    req = urllib.request.Request(endpoint, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        value = json.loads(json.loads(r.read())["message"]["content"])
    return {k: value.get(k) for k in ("relevance", "faithfulness", "helpfulness", "tone", "comment")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="không gọi model; bỏ qua case cần model")
    ap.add_argument("--category", action="append", help="chỉ chạy nhóm này (lặp được)")
    ap.add_argument("--case", action="append", help="chỉ chạy case id này")
    ap.add_argument("--repeat", type=int, default=1, help="chạy lặp để đo độ ổn định")
    ap.add_argument("--judge", action="store_true", help="chấm thêm bằng LLM (model nặng)")
    ap.add_argument("--light", help="model nhẹ (mặc định theo local.env)")
    ap.add_argument("--heavy", help="model nặng (mặc định theo local.env)")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--fail-on-regression", action="store_true")
    args = ap.parse_args()

    env = load_env()
    light = args.light or env.get("LIGHT_MODEL", "qwen3.8:27b")
    heavy = args.heavy or env.get("HEAVY_MODEL", "qwen3.8:27b")
    if args.offline:
        os.environ["NEMO_GUARDRAILS"] = "0"
    else:
        os.environ.setdefault("LOCAL_HEAVY_MODEL", heavy)

    import agent
    import catalog as catalog_store
    import catalog_sql
    from evals.graders import grade
    from observability import TraceStore
    from orchestrator import Orchestrator

    if not agent.all_records():
        agent.load_records(ROOT / "data" / "mock_knowledge.json")
    records = agent.all_records()
    items = catalog_store.load_catalog()
    ctx = {"records": {r["id"]: r for r in records}, "items": {i["id"]: i for i in items},
           "sql": catalog_sql.build_db(items), "offline": args.offline}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = EVALS / "results"
    out_dir.mkdir(exist_ok=True)
    tracer = TraceStore(out_dir / f"{stamp}.traces.jsonl")
    orchestrator = None if args.offline else Orchestrator(light, tracer=tracer)
    model = "" if args.offline else light

    cases = [json.loads(l) for l in (EVALS / "cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.category:
        cases = [c for c in cases if c["category"] in args.category]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]

    runs, skipped = [], []
    print(f"Chạy {len(cases)} case × {args.repeat} — {'offline' if args.offline else f'live ({light} / {heavy})'}")
    for case in cases:
        if args.offline and needs_model(case):
            skipped.append(case["id"]); continue
        for attempt in range(args.repeat):
            t0 = time.perf_counter()
            try:
                result = agent.ask(case["question"], case["product"], records, model, history=case.get("history"),
                                   orchestrator=orchestrator, catalog=items, tracer=tracer, actor="eval")
                graded = grade(case, result, ctx)
            except Exception as exc:  # agent lỗi là một kết quả trượt, không dừng cả bộ
                result, graded = {"status": "exception", "answer": repr(exc)}, {
                    "passed": False, "errors": [f"exception: {exc!r}"], "invariant_violations": 0}
            latency = time.perf_counter() - t0
            run = {"id": case["id"], "category": case["category"], "attempt": attempt, "latency_s": round(latency, 2),
                   "status": result.get("status"), "reason": result.get("reason"),
                   "tier": result.get("model_tier"), "answer": (result.get("answer") or "")[:400],
                   "citations": [c.get("id") for c in result.get("citations") or []][:6], **graded}
            if args.judge and not args.offline and result.get("status") not in ("exception",):
                try:
                    run["judge"] = judge(heavy, case["question"], result)
                except Exception as exc:
                    run["judge_error"] = repr(exc)
            runs.append(run)
            mark = "✓" if graded["passed"] else "✗"
            print(f"  {mark} {case['id']:<24} {latency:6.1f}s  {result.get('status')}"
                  + ("" if graded["passed"] else "  ← " + "; ".join(graded["errors"])[:150]), flush=True)

    # ------------------------------------------------------------ tổng hợp
    by_case = defaultdict(list)
    for r in runs:
        by_case[r["id"]].append(r)
    case_pass = {cid: all(r["passed"] for r in rs) for cid, rs in by_case.items()}
    flaky = sorted(cid for cid, rs in by_case.items() if len({r["passed"] for r in rs}) > 1)
    cats = defaultdict(lambda: {"cases": 0, "passed": 0, "latency": []})
    for r in runs:
        cats[r["category"]]["latency"].append(r["latency_s"])
    for cid, rs in by_case.items():
        cats[rs[0]["category"]]["cases"] += 1
        cats[rs[0]["category"]]["passed"] += case_pass[cid]
    latencies = [r["latency_s"] for r in runs]
    judged = [r["judge"] for r in runs if r.get("judge")]
    judge_avg = {k: round(statistics.mean(j[k] for j in judged if isinstance(j.get(k), (int, float))), 2)
                 for k in ("relevance", "faithfulness", "helpfulness", "tone")
                 if any(isinstance(j.get(k), (int, float)) for j in judged)}
    summary = {
        "timestamp": stamp, "mode": "offline" if args.offline else "live", "models": {"light": light, "heavy": heavy},
        "cases": len(by_case), "passed": sum(case_pass.values()), "skipped": skipped,
        "pass_rate": round(sum(case_pass.values()) / max(1, len(by_case)), 3),
        "invariant_violations": sum(r["invariant_violations"] for r in runs),
        "latency_p50_s": round(percentile(latencies, 50), 2), "latency_p95_s": round(percentile(latencies, 95), 2),
        "tiers": dict(Counter(r["tier"] or "none" for r in runs)), "flaky": flaky, "judge": judge_avg,
        "categories": {k: {"cases": v["cases"], "passed": v["passed"],
                           "pass_rate": round(v["passed"] / max(1, v["cases"]), 3),
                           "p50_s": round(percentile(v["latency"], 50), 2),
                           "p95_s": round(percentile(v["latency"], 95), 2)} for k, v in sorted(cats.items())},
        "case_pass": case_pass,
    }
    baseline_path = EVALS / "baseline.json"
    regressions, fixes = [], []
    if baseline_path.exists():
        base = json.loads(baseline_path.read_text())["case_pass"]
        regressions = sorted(c for c, ok in case_pass.items() if base.get(c) and not ok)
        fixes = sorted(c for c, ok in case_pass.items() if c in base and not base[c] and ok)
    summary.update(regressions=regressions, fixes=fixes)
    (out_dir / f"{stamp}.json").write_text(json.dumps({"summary": summary, "runs": runs}, ensure_ascii=False, indent=1))
    md = render_markdown(summary, runs)
    (out_dir / f"{stamp}.md").write_text(md)
    print("\n" + md.split("## Case trượt")[0])
    print(f"Báo cáo: evals/results/{stamp}.md")
    if args.save_baseline:
        baseline_path.write_text(json.dumps({"summary": {k: v for k, v in summary.items() if k != "case_pass"},
                                             "case_pass": case_pass}, ensure_ascii=False, indent=1))
        print("Đã lưu baseline: evals/baseline.json")
    if args.fail_on_regression and regressions:
        sys.exit(1)


def render_markdown(s, runs):
    lines = [f"# Eval agent — {s['timestamp']} ({s['mode']})", "",
             f"- Model: nhẹ `{s['models']['light']}`, nặng `{s['models']['heavy']}`",
             f"- **Đạt {s['passed']}/{s['cases']} case ({s['pass_rate']:.0%})**"
             + (f", bỏ qua {len(s['skipped'])} case cần model" if s["skipped"] else ""),
             f"- Vi phạm invariant (lỗi nghiêm trọng): **{s['invariant_violations']}**",
             f"- Độ trễ p50 {s['latency_p50_s']}s • p95 {s['latency_p95_s']}s • tầng model: {s['tiers']}"]
    if s["flaky"]:
        lines.append(f"- Không ổn định giữa các lần chạy: {', '.join(s['flaky'])}")
    if s["judge"]:
        lines.append("- LLM-judge (1-5): " + ", ".join(f"{k} {v}" for k, v in s["judge"].items()))
    if s.get("regressions"):
        lines.append(f"- ⚠ Hồi quy so với baseline: {', '.join(s['regressions'])}")
    if s.get("fixes"):
        lines.append(f"- Đã sửa so với baseline: {', '.join(s['fixes'])}")
    lines += ["", "| Nhóm | Đạt | Tỉ lệ | p50 | p95 |", "|---|---|---|---|---|"]
    for k, v in s["categories"].items():
        lines.append(f"| {k} | {v['passed']}/{v['cases']} | {v['pass_rate']:.0%} | {v['p50_s']}s | {v['p95_s']}s |")
    lines += ["", "## Case trượt", ""]
    seen = set()
    for r in runs:
        if not r["passed"] and r["id"] not in seen:
            seen.add(r["id"])
            lines.append(f"- **{r['id']}** ({r['category']}, {r['status']}/{r['reason']}): {'; '.join(r['errors'])}")
            lines.append(f"  - Trả lời: {r['answer'][:200]!r}")
    if not seen:
        lines.append("Không có.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
