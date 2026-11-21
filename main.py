import json
import logging
import re
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

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
# 1. ADAPTADOR ANTIGRAVITY (Agentes Argon e 3.8)
# ==============================================================================
def extract_java_code(raw: str) -> str:
    match = re.search(r"```(?:java)?\s*(.*?)```", raw, re.DOTALL)
    return match.group(1).strip() if match else raw.strip()


def run_antigravity_agent(agent_role: str, prompt: str, working_dir: str) -> str:
    """
    Executa um agente configurado no orca.config.yaml (ex.: Argon ou 3.8) via Antigravity.
    """
    agent_cfg = config["agents"][agent_role]
    model_name = agent_cfg["model"]  # Ex: "argon" ou "3.8"
    cmd_template = agent_cfg.get(
        "command",
        ["antigravity", "run", "--model", "{model}", "--prompt", "{prompt}"],
    )

    cmd = [
        part.format(model=model_name, prompt=prompt)
        for part in cmd_template
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
        raise RuntimeError(f"Erro no agente Antigravity ({model_name}): {err}")

    return res.stdout.strip()


def invoke_refactor_agent(
    file_path: str,
    source_code: str,
    violations: list[dict],
    repo_root: str,
    feedback: str | None = None,
) -> str:
    smells_desc = "\n".join(
        f"- {v['rule']} (Linhas {v['begin_line']}-{v['end_line']}): {v['description']}"
        for v in violations
    )

    prompt = (
        f"Você é um engenheiro especialista em refatoração Java.\n"
        f"Arquivo alvo: {Path(file_path).name}\n\n"
        f"Code Smells detectados pelo PMD:\n{smells_desc}\n\n"
        f"Regras obrigatórias:\n"
        f"1. Elimine todos os code smells listados sem introduzir novos.\n"
        f"2. Preserve 100% do comportamento funcional e compatibilidade com os testes JUnit.\n"
        f"3. Retorne EXCLUSIVAMENTE o código-fonte completo do arquivo dentro de um bloco ```java.\n\n"
        f"Código atual:\n```java\n{source_code}\n```"
    )

    if feedback:
        prompt += (
            f"\n\nATENÇÃO — A tentativa anterior foi REJEITADA pelo Quality Gate:\n"
            f"{feedback}\nCorrija o problema apontado mantendo a eliminação dos smells."
        )

    raw_output = run_antigravity_agent("refactorer", prompt, repo_root)
    return extract_java_code(raw_output)


def invoke_semantic_reviewer(
    diff_text: str,
    target_smells: list[str],
    repo_root: str,
) -> tuple[bool, str]:
    prompt = (
        f"Você é um revisor técnico de refatorações Java. Analise o git diff abaixo e verifique:\n"
        f"1. Se a refatoração preserva a semântica original e as regras de negócio.\n"
        f"2. Se não há supressões artificiais (@SuppressWarnings) ou remoção indevida de lógica.\n\n"
        f"Smells alvo: {', '.join(target_smells)}\n\n"
        f"Git Diff:\n```diff\n{diff_text}\n```\n\n"
        f"Responda APENAS em JSON válido: "
        f'{{"approved": true/false, "feedback": "justificativa objetiva"}}'
    )

    raw = run_antigravity_agent("reviewer", prompt, repo_root)
    cleaned = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

    try:
        data = json.loads(cleaned)
        return bool(data.get("approved", False)), str(data.get("feedback", ""))
    except json.JSONDecodeError:
        return ('"approved": true' in cleaned.lower()), cleaned


# ==============================================================================
# 2. QUALITY GATE (Fiel ao orca.config.yaml + Maven + Revisor 3.8)
# ==============================================================================
def evaluate_quality_gate(
    baseline: dict,
    current: dict,
    target_smells: list[str],
    repo_root: str,
    diff_text: str,
) -> tuple[bool, str]:
    # Barreira 1: Testes unitários Maven
    tests_ok, test_log = run_maven_tests(repo_root)
    if not tests_ok:
        return False, f"Falha nos testes Maven:\n{test_log}"

    gate_cfg = config["quality_gate"]
    reasons = []

    # Barreira 2.1: Eliminação dos smells alvo
    if gate_cfg.get("require_smell_removal", True):
        persisting = [s for s in target_smells if s in current["smells"]]
        if persisting:
            reasons.append(f"Smell(s) persistente(s): {', '.join(persisting)}.")

    # Barreira 2.2: Bloqueio de novos smells
    if not gate_cfg.get("allow_new_smells", False):
        new_smells = set(current["smells"]) - set(baseline["smells"])
        if new_smells:
            reasons.append(f"Novos smells introduzidos: {', '.join(new_smells)}.")

    # Barreira 2.3: Métricas estruturais CK
    for metric, operator in gate_cfg.get("metrics_rules", {}).items():
        old_val = baseline["metrics"][metric]
        new_val = current["metrics"][metric]

        if operator == "lt" and not (new_val < old_val):
            reasons.append(f"Métrica {metric} não reduziu ({old_val} -> {new_val}).")
        elif operator == "le" and not (new_val <= old_val):
            reasons.append(f"Métrica {metric} piorou ({old_val} -> {new_val}).")

    if reasons:
        return False, " | ".join(reasons)

    # Barreira 3: Auditoria Semântica (Agente 3.8)
    if not diff_text.strip():
        return False, "Nenhuma alteração detectada no arquivo (diff vazio)."

    approved, review_msg = invoke_semantic_reviewer(diff_text, target_smells, repo_root)
    if not approved:
        return False, f"Revisor (3.8) rejeitou: {review_msg}"

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
# 4. PIPELINE ORCA
# ==============================================================================
def main() -> None:
    repo_cfg = config["repository"]
    repo_root = repo_cfg["root"]
    source_dir = repo_cfg["source_dir"]
    max_iterations = config["orchestrator"].get("max_iterations", 3)

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

    target_files = list(violations_by_file.keys())
    logger.info(
        f"Baseline: {len(repo_baseline['violations_detail'])} violações em "
        f"{len(target_files)} arquivos."
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

            refactored_code = invoke_refactor_agent(
                file_path=file_path,
                source_code=original_code,
                violations=file_violations,
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
        "files_total": len(target_files),
        "baseline_metrics": repo_baseline["metrics"],
        "final_metrics": final_analysis["metrics"],
        "records": records,
    }

    with open(results_dir / "experiment_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info(
        f"Concluído! Violações: {report['initial_violations']} -> {report['final_violations']} "
        f"| Aprovados: {report['files_approved']}/{report['files_total']}"
    )


if __name__ == "__main__":
    main()