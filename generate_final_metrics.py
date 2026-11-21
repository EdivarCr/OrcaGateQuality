import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from analyzers.static_runner import run_analysis

REPO_DIR = ROOT_DIR / "workspace" / "LLMCodeSmellRefactor"
SRC_DIR = REPO_DIR / "src" / "main" / "java"
RESULTS_DIR = ROOT_DIR / "results"
BASELINE_COMMIT = "1de20ef"
FINAL_BRANCH = "experiment/orca"
METRICS_KEYS = ["CBO", "WMC", "LCOM", "CyclomaticComplexity"]


def collect_metrics(label: str) -> dict:
    files = sorted(SRC_DIR.rglob("*.java"))
    data = {}
    for idx, java_file in enumerate(files, 1):
        print(f"\r[{label}] {idx}/{len(files)}: {java_file.name:30s}", end="", flush=True)
        res = run_analysis(str(java_file), str(REPO_DIR))
        data[java_file.name] = {
            "path": str(java_file.relative_to(REPO_DIR)),
            "metrics": res["metrics"],
            "violations": len(res["violations_detail"]),
            "smells": sorted({v["rule"] for v in res["violations_detail"]}),
        }
    print()
    return data


def summarize_group(before: dict, after: dict, file_names: list[str]) -> dict:
    summary = {}
    for m in METRICS_KEYS:
        b_vals = [before[f]["metrics"][m] for f in file_names if m in before[f]["metrics"]]
        a_vals = [after[f]["metrics"][m] for f in file_names if m in after[f]["metrics"]]
        summary[m] = {
            "mean_before": round(statistics.mean(b_vals), 2),
            "mean_after": round(statistics.mean(a_vals), 2),
            "median_before": round(statistics.median(b_vals), 2),
            "median_after": round(statistics.median(a_vals), 2),
            "stdev_before": round(statistics.stdev(b_vals), 2) if len(b_vals) > 1 else 0.0,
            "stdev_after": round(statistics.stdev(a_vals), 2) if len(a_vals) > 1 else 0.0,
            "max_before": max(b_vals),
            "max_after": max(a_vals),
        }
    return summary


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)

    # 1. Coleta o estado final (branch experiment/orca)
    subprocess.run(["git", "-C", str(REPO_DIR), "checkout", "-q", FINAL_BRANCH], check=True)
    after = collect_metrics("DEPOIS")

    # 2. Coleta o baseline original (commit 1de20ef) e restaura a branch final
    subprocess.run(["git", "-C", str(REPO_DIR), "checkout", "-q", BASELINE_COMMIT], check=True)
    try:
        before = collect_metrics("ANTES ")
    finally:
        subprocess.run(["git", "-C", str(REPO_DIR), "checkout", "-q", FINAL_BRANCH], check=True)

    all_files = sorted(before.keys())
    refactored_files = [f for f in all_files if before[f]["violations"] > 0]

    report = {
        "baseline_commit": BASELINE_COMMIT,
        "final_branch": FINAL_BRANCH,
        "total_classes": len(all_files),
        "refactored_classes_count": len(refactored_files),
        "total_violations": {
            "before": sum(before[f]["violations"] for f in all_files),
            "after": sum(after[f]["violations"] for f in all_files),
        },
        "all_classes": summarize_group(before, after, all_files),
        "refactored_only": summarize_group(before, after, refactored_files),
        "per_refactored_file": {
            f: {
                "target_smells": before[f]["smells"],
                "before": before[f]["metrics"],
                "after": after[f]["metrics"],
            }
            for f in refactored_files
        },
    }

    # Salva JSON consolidado
    json_out = RESULTS_DIR / "final_metrics_comparison.json"
    json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # Salva CSV por classe (ideal para tabelas e gráficos no artigo)
    csv_out = RESULTS_DIR / "metrics_per_class.csv"
    with csv_out.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow([
            "file", "was_refactored", "target_smells",
            "CBO_before", "CBO_after", "WMC_before", "WMC_after",
            "LCOM_before", "LCOM_after", "CC_before", "CC_after",
        ])
        for f in all_files:
            bm, am = before[f]["metrics"], after[f]["metrics"]
            writer.writerow([
                f,
                f in refactored_files,
                ";".join(before[f]["smells"]),
                bm.get("CBO", ""), am.get("CBO", ""),
                bm.get("WMC", ""), am.get("WMC", ""),
                bm.get("LCOM", ""), am.get("LCOM", ""),
                bm.get("CyclomaticComplexity", ""), am.get("CyclomaticComplexity", ""),
            ])

    print(f"\nRelatórios gerados com sucesso:\n - {json_out}\n - {csv_out}")


if __name__ == "__main__":
    main()