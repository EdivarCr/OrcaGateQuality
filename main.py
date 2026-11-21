import shutil  # garanta que esteja nos imports do topo
import json
import logging
import re
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

from analyzers.static_runner import run_analysis, run_maven_tests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("orca_execution.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("ORCA")

with open("orca.config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# ==============================================================================
# 1. INVOCAÇÃO DOS AGENTES ANTIGRAVITY (Argon e 3.8)
# ==============================================================================
def extract_java_code(raw: str) -> str:
    match = re.search(r"```(?:java)?\s*(.*?)```", raw, re.DOTALL)
    return match.group(1).strip() if match else raw.strip()

def extract_java_code(raw: str, expected_class: str | None = None) -> str:
    blocks = re.findall(r"```(?:java)?\s*(.*?)```", raw, re.DOTALL)
    candidates = [b.strip() for b in blocks] if blocks else [raw.strip()]

    for code in sorted(candidates, key=len, reverse=True):
        has_package = "package " in code
        has_type = any(kw in code for kw in ("class ", "interface ", "enum ", "record "))
        has_expected = (expected_class in code) if expected_class else True
        if has_package and has_type and has_expected:
            return code

    raise ValueError(
        f"Resposta do agente não contém código Java válido para '{expected_class}'. "
        f"Início da saída: {raw[:200]!r}"
    )

def run_antigravity_agent(agent_key: str, prompt: str, working_dir: str) -> str:
    agy_bin = shutil.which("agy")
    if not agy_bin:
        raise FileNotFoundError("Executável 'agy' não encontrado no PATH.")

    agent_cfg = config["agents"][agent_key]
    model = agent_cfg["model"].strip()

    cmd = [
        agy_bin,
        "--model", model,
        "--output-format", "text",
        "--disable-slash-commands",
        f"--print={prompt}",
    ]

    res = subprocess.run(
        cmd,
        cwd=str(Path(working_dir).resolve()),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    if res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip()
        raise RuntimeError(f"Erro no agente Antigravity ({model}): {err}")

    return res.stdout.strip()


def invoke_refactorer(
    file_path: str,
    source_code: str,
    violations: list[dict],
    metrics: dict[str, float],
    repo_root: str,
    feedback: str | None = None,
) -> str:
    agent_cfg = config["agents"]["refactorer"]
    system_prompt = agent_cfg["system_prompt"].strip()

    smells_desc = "\n".join(
        f"- {v['rule']} (Linhas {v['begin_line']}-{v['end_line']}): {v['description']}"
        for v in violations
    )
    metrics_desc = ", ".join(f"{k}={v}" for k, v in metrics.items())

    prompt = (
        f"{system_prompt}\n\n"
        f"Arquivo alvo: {Path(file_path).name}\n"
        f"Métricas atuais (CK): {metrics_desc}\n\n"
        f"Diagnóstico do Code Smell (PMD):\n{smells_desc}\n\n"
        f"Código-fonte atual:\n```java\n{source_code}\n```"
    )

    if feedback:
        prompt += (
            f"\n\nATENÇÃO — A tentativa anterior foi REJEITADA pelo Quality Gate:\n"
            f"{feedback}\nAjuste a refatoração para corrigir todos os pontos acima."
        )

    raw_output = run_antigravity_agent("refactorer", prompt, repo_root)
    return extract_java_code(raw_output)


def invoke_reviewer(
    original_code: str,
    refactored_code: str,
    diff_text: str,
    target_smells: list[str],
    repo_root: str,
) -> tuple[bool, str]:
    agent_cfg = config["agents"]["reviewer"]
    system_prompt = agent_cfg["system_prompt"].strip()

    prompt = (
        f"{system_prompt}\n\n"
        f"Smells alvo eliminados: {', '.join(target_smells)}\n\n"
        f"Git Diff:\n```diff\n{diff_text}\n```\n\n"
        f"Código Original:\n```java\n{original_code}\n```\n\n"
        f"Código Refatorado:\n```java\n{refactored_code}\n```"
    )

    raw = run_antigravity_agent("reviewer", prompt, repo_root)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

    try:
        data = json.loads(cleaned)
        return bool(data.get("approved", False)), str(data.get("reason", ""))
    except json.JSONDecodeError:
        return ('"approved": true' in cleaned.lower()), cleaned


# ==============================================================================
# 2. QUALITY GATE (Alinhado a 'quality_gate' do orca.config.yaml)
# ==============================================================================
def evaluate_quality_gate(
    baseline: dict,
    current: dict,
    target_smells: list[str],
    repo_root: str,
    original_code: str,
    refactored_code: str,
    diff_text: str,
) -> tuple[bool, str]:
    gate_cfg = config["quality_gate"]

    # 1. Testes unitários Maven
    if gate_cfg.get("require_tests_pass", True):
        tests_ok, test_log = run_maven_tests(repo_root)
        if not tests_ok:
            return False, f"Falha nos testes Maven:\n{test_log}"

    reasons = []

    # 2. Eliminação dos smells alvo
    if gate_cfg.get("require_smell_removal", True):
        persisting = [s for s in target_smells if s in current["smells"]]
        if persisting:
            reasons.append(f"Smell(s) persistente(s): {', '.join(persisting)}.")

    # 3. Bloqueio de novos smells
    if not gate_cfg.get("allow_new_smells", False):
        new_smells = set(current["smells"]) - set(baseline["smells"])
        if new_smells:
            reasons.append(f"Novos smells introduzidos: {', '.join(new_smells)}.")

    # 4. Regras de métricas estruturais CK
    for metric, operator in gate_cfg.get("metrics_rules", {}).items():
        old_val = baseline["metrics"][metric]
        new_val = current["metrics"][metric]

        if operator == "lt" and not (new_val < old_val):
            reasons.append(f"Métrica {metric} não reduziu ({old_val} -> {new_val}).")
        elif operator == "le" and not (new_val <= old_val):
            reasons.append(f"Métrica {metric} piorou ({old_val} -> {new_val}).")

    if reasons:
        return False, " | ".join(reasons)

    # 5. Auditoria Semântica (Agente 3.8)
    if not diff_text.strip():
        return False, "Nenhuma modificação foi realizada no arquivo (diff vazio)."

    approved, reason = invoke_reviewer(
        original_code=original_code,
        refactored_code=refactored_code,
        diff_text=diff_text,
        target_smells=target_smells,
        repo_root=repo_root,
    )
    if not approved:
        return False, f"Revisor (3.8) rejeitou: {reason}"

    return True, "Aprovado em todos os critérios de qualidade."


# ==============================================================================
# 3. OPERAÇÕES GIT NO REPOSITÓRIO ALVO
# ==============================================================================
def run_git(repo_root: str, args: list[str]) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(Path(repo_root).resolve()),
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Erro Git ({' '.join(args)}): {res.stderr.strip()}")
    return res.stdout.strip()


def get_file_diff(repo_root: str, file_path: str) -> str:
    return run_git(repo_root, ["diff", "--", str(Path(file_path).resolve())])


def revert_file(repo_root: str, file_path: str) -> None:
    run_git(repo_root, ["checkout", "--", str(Path(file_path).resolve())])


def commit_file(repo_root: str, file_path: str, message: str) -> None:
    resolved = str(Path(file_path).resolve())
    run_git(repo_root, ["add", resolved])
    run_git(repo_root, ["commit", "-m", message])


# ==============================================================================
# 4. ORQUESTRADOR PRINCIPAL
# ==============================================================================
def main() -> None:
    orch_cfg = config["orchestrator"]
    repo_root = orch_cfg["target_repo"]
    source_dir = orch_cfg["source_dir"]
    max_iterations = orch_cfg.get("max_iterations", 2)
    max_files = orch_cfg.get("max_files_per_run", 5)

    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)

    logger.info("=== FASE 0: Extraindo Baseline e Validando Testes ===")
    tests_ok, test_msg = run_maven_tests(repo_root)
    if not tests_ok:
        raise RuntimeError(f"Baseline falhou nos testes:\n{test_msg}")

    repo_baseline = run_analysis(source_dir, repo_root)
    with open(results_dir / "baseline.json", "w", encoding="utf-8") as f:
        json.dump(repo_baseline, f, indent=2, ensure_ascii=False)

    violations_by_file: dict[str, list[dict]] = defaultdict(list)
    for v in repo_baseline["violations_detail"]:
        violations_by_file[v["file"]].append(v)

    all_affected = list(violations_by_file.keys())
    target_files = all_affected[:max_files]
    logger.info(
        f"Baseline: {len(repo_baseline['violations_detail'])} violações em "
        f"{len(all_affected)} arquivos. Lote atual: {len(target_files)} arquivo(s)."
    )

    records = []

    for idx, file_path in enumerate(target_files, start=1):
        file_violations = violations_by_file[file_path]
        target_smells = list({v["rule"] for v in file_violations})
        file_name = Path(file_path).name

        logger.info(f"\n[{idx}/{len(target_files)}] Arquivo: {file_name} | Alvo: {target_smells}")

        file_baseline = run_analysis(file_path, repo_root)
        original_code = Path(file_path).read_text(encoding="utf-8")

        feedback: str | None = None
        approved = False
        attempts = 0

        for attempt in range(1, max_iterations + 1):
            attempts = attempt
            logger.info(f"  -> Iteração {attempt}/{max_iterations} (Agente Argon)...")
            Path(file_path).write_text(original_code, encoding="utf-8")

            refactored_code = invoke_refactorer(
                file_path=file_path,
                source_code=original_code,
                violations=file_violations,
                metrics=file_baseline["metrics"],
                repo_root=repo_root,
                feedback=feedback,
            )
            Path(file_path).write_text(refactored_code, encoding="utf-8")

            current_analysis = run_analysis(file_path, repo_root)
            diff_text = get_file_diff(repo_root, file_path)

            passed, gate_msg = evaluate_quality_gate(
                baseline=file_baseline,
                current=current_analysis,
                target_smells=target_smells,
                repo_root=repo_root,
                original_code=original_code,
                refactored_code=refactored_code,
                diff_text=diff_text,
            )

            if passed:
                msg = f"refactor({file_name}): remove {', '.join(target_smells)} via ORCA"
                commit_file(repo_root, file_path, msg)
                logger.info(f"  [APROVADO] {msg}")
                approved = True
                break

            logger.warning(f"  [REJEITADO] {gate_msg}")
            feedback = gate_msg

        if not approved:
            revert_file(repo_root, file_path)
            logger.error(f"  [ROLLBACK] {file_name} restaurado após {max_iterations} tentativas.")

        records.append({
            "file": file_path,
            "target_smells": target_smells,
            "approved": approved,
            "attempts": attempts,
            "last_feedback": feedback,
        })

    logger.info("\n=== FASE FINAL: Coletando Métricas Pós-Execução ===")
    final_analysis = run_analysis(source_dir, repo_root)

    report = {
        "timestamp": datetime.now().isoformat(),
        "initial_violations": len(repo_baseline["violations_detail"]),
        "final_violations": len(final_analysis["violations_detail"]),
        "files_approved": sum(1 for r in records if r["approved"]),
        "files_processed": len(target_files),
        "baseline_metrics": repo_baseline["metrics"],
        "final_metrics": final_analysis["metrics"],
        "records": records,
    }

    with open(results_dir / "experiment_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info(
        f"Lote concluído! Violações totais no repo: {report['initial_violations']} -> "
        f"{report['final_violations']} | Aprovados no lote: {report['files_approved']}/{report['files_processed']}"
    )


if __name__ == "__main__":
    main()