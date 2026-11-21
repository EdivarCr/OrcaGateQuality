import csv
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


PMD_BIN = Path("./tools/pmd/bin/pmd").resolve()
CK_JAR = Path("./tools/ck.jar").resolve()
PMD_RULESET = "category/java/design.xml"  # Contém GodClass, ExcessiveParameterList, CognitiveComplexity, etc.


def run_maven_tests(repo_root: str) -> tuple[bool, str]:
    """
    Executa a suíte JUnit existente em src/test/java via Maven.
    Retorna (passou: bool, log_erro: str).
    """
    result = subprocess.run(
        ["mvn", "test", "-B"],
        cwd=str(Path(repo_root).resolve()),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, "Todos os testes unitários passaram."
    
    # Extrai as últimas linhas do log do Maven para alimentar o feedback do Agente A
    error_tail = "\n".join(result.stdout.splitlines()[-25:])
    return False, f"Falha na execução do 'mvn test':\n{error_tail}"

def run_pmd(target_path: str, repo_root: str, output_json: str) -> list[dict]:
    ruleset_path = str((Path(repo_root) / "rules.xml").resolve())
    cmd = [
        str(PMD_BIN),
        "check",
        "-d", str(Path(target_path).resolve()),
        "-R", ruleset_path,
        "-f", "json",
        "-r", output_json,
        "--no-cache",
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 4):
        raise RuntimeError(f"Erro ao executar PMD (code {result.returncode}): {result.stderr}")

    if not os.path.exists(output_json):
        return []

    with open(output_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    violations = []
    for file_entry in data.get("files", []):
        filename = file_entry.get("filename")
        for v in file_entry.get("violations", []):
            violations.append({
                "file": filename,
                "rule": v.get("rule"),
                "description": v.get("description"),
                "begin_line": v.get("beginline"),
                "end_line": v.get("endline"),
                "priority": v.get("priority"),
            })

    return violations


def run_ck(target_dir: str, output_prefix: str, target_file: str | None = None) -> dict[str, float]:
    """
    Executa a ferramenta CK sobre o diretório do projeto/pacote e extrai as métricas
    estruturais (CBO, WMC, LCOM e Complexidade Ciclomática máxima dos métodos).
    """
    cmd = [
        "java",
        "-jar", str(CK_JAR),
        str(Path(target_dir).resolve()),
        "false",  # use_jars
        "0",      # max_files_per_partition (0 = automático)
        "false",  # variables_and_fields_metrics
        output_prefix,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Erro ao executar CK: {result.stderr}")

    class_csv = f"{output_prefix}class.csv"
    method_csv = f"{output_prefix}method.csv"

    metrics = {
        "CBO": 0.0,
        "WMC": 0.0,
        "LCOM": 0.0,
        "CyclomaticComplexity": 0.0,
    }

    target_resolved = str(Path(target_file).resolve()) if target_file else None

    # 1. Extrai métricas em nível de classe (CBO, WMC, LCOM)
    if os.path.exists(class_csv):
        with open(class_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if target_resolved and str(Path(row["file"]).resolve()) != target_resolved:
                    continue
                metrics["CBO"] = max(metrics["CBO"], float(row.get("cbo", 0)))
                metrics["WMC"] = max(metrics["WMC"], float(row.get("wmc", 0)))
                metrics["LCOM"] = max(metrics["LCOM"], float(row.get("lcom", 0)))

    # 2. Extrai a maior Complexidade Ciclomática (campo 'wmc' no method.csv do CK)
    if os.path.exists(method_csv):
        with open(method_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if target_resolved and str(Path(row["file"]).resolve()) != target_resolved:
                    continue
                method_cc = float(row.get("wmc", 0))
                metrics["CyclomaticComplexity"] = max(metrics["CyclomaticComplexity"], method_cc)

    return metrics


def run_analysis(target_path: str, repo_root: str) -> dict[str, Any]:
    """
    Ponto de entrada unificado chamado pelo ORCA na Fase 0 (Baseline) e no Quality Gate.
    Retorna os smells detectados e o dicionário de métricas compatível com o orca.config.yaml.
    """
    is_file = os.path.isfile(target_path)
    ck_scan_dir = os.path.dirname(target_path) if is_file else target_path
    filter_file = target_path if is_file else None

    with tempfile.TemporaryDirectory() as tmp_dir:
        pmd_out = os.path.join(tmp_dir, "pmd.json")
        ck_prefix = os.path.join(tmp_dir, "ck_")

        violations = run_pmd(target_path, pmd_out)
        metrics = run_ck(ck_scan_dir, ck_prefix, target_file=filter_file)

    return {
        "smells": list({v["rule"] for v in violations}),  # Nomes únicos para o Quality Gate
        "violations_detail": violations,                  # Detalhes (linhas/descrição) para o Agente A
        "metrics": metrics,                               # Valores numéricos para cálculo de Delta
    }