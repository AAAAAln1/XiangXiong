#!/usr/bin/env python3
"""
validate_persistence.py — v0.3 认知持久化验证工具

离线衡量 XiangXiong 的持久化能力：
- evidence coverage（证据完整性）
- hypothesis lifecycle completion（假设生命周期闭环）
- experiment -> memory 转化率（认知沉淀）

不干预运行时，只读取归档数据验证。
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

# 添加 runtime 到路径
_SCRIPT_DIR = Path(__file__).parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_RUNTIME_DIR = _PROJECT_ROOT / "runtime"
sys.path.insert(0, str(_RUNTIME_DIR))

try:
    from world import parse as world_parse
except ImportError:
    # 如果从项目外运行，尝试相对导入
    sys.path.insert(0, str(_PROJECT_ROOT))
    from runtime.world import parse as world_parse


def find_project_root() -> Path:
    """定位项目根目录（包含 xiangxiong/ 的目录）"""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "xiangxiong").exists():
            return current
        current = current.parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = find_project_root()
XIANGXIONG_ROOT = PROJECT_ROOT / "xiangxiong"
RESULTS_DIR = XIANGXIONG_ROOT / "results"


def load_experiments(target: str) -> List[Dict]:
    """加载目标的所有实验（支持两种目录结构）"""
    # 新版：experiments/<target>/
    exp_dir_new = XIANGXIONG_ROOT / "experiments" / target
    # 旧版：results/<target>/experiments/
    exp_dir_old = RESULTS_DIR / target / "experiments"
    
    exp_dir = exp_dir_new if exp_dir_new.exists() else exp_dir_old
    
    if not exp_dir.exists():
        return []
    
    experiments = []
    for exp_path in exp_dir.glob("exp-*/experiment.json"):
        try:
            with open(exp_path, encoding="utf-8") as f:
                exp = json.load(f)
                exp["_path"] = str(exp_path)
                experiments.append(exp)
        except Exception as e:
            print(f"⚠️  无法加载 {exp_path}: {e}")
    
    return experiments


def load_target_memory(target: str) -> Dict:
    """加载目标的归档 memory"""
    memory_path = RESULTS_DIR / target / "memory.md"
    if not memory_path.exists():
        return {"objects": [], "constraints": [], "unknowns": [], "counters": []}
    
    try:
        with open(memory_path, encoding="utf-8") as f:
            content = f.read()
            lines = content.splitlines() if content else []
            view = world_parse(lines)
            return {
                "objects": view.objects,
                "constraints": view.constraints,
                "unknowns": view.unknowns,
                "counters": view.counters,
                "target": view.target,
            }
    except Exception as e:
        print(f"⚠️  无法解析 {memory_path}: {e}")
        return {"objects": [], "constraints": [], "unknowns": [], "counters": []}


def validate_evidence_coverage(experiments: List[Dict]) -> Dict:
    """
    指标 1: Evidence Coverage
    
    检查：
    - experiment.json 存在
    - evidence.md 存在且非空
    - observations 数量 > 0
    - artifact 文件可访问
    """
    total = len(experiments)
    has_evidence = 0
    has_observations = 0
    has_artifacts = 0
    missing_files = []
    
    for exp in experiments:
        exp_dir = Path(exp["_path"]).parent
        
        # 检查 evidence.md
        evidence_path = exp_dir / "evidence.md"
        if evidence_path.exists() and evidence_path.stat().st_size > 0:
            has_evidence += 1
        else:
            missing_files.append(f"{exp['id']}/evidence.md")
        
        # 检查 observations
        if exp.get("observations") and len(exp["observations"]) > 0:
            has_observations += 1
        
        # 检查 artifacts
        artifacts = exp.get("artifacts", [])
        if artifacts:
            artifact_exists = any(
                (XIANGXIONG_ROOT / art).exists() for art in artifacts
            )
            if artifact_exists:
                has_artifacts += 1
    
    return {
        "total": total,
        "has_evidence": has_evidence,
        "has_observations": has_observations,
        "has_artifacts": has_artifacts,
        "evidence_coverage": has_evidence / total if total > 0 else 0,
        "observation_coverage": has_observations / total if total > 0 else 0,
        "artifact_coverage": has_artifacts / total if total > 0 else 0,
        "missing_files": missing_files,
    }


def validate_hypothesis_lifecycle(experiments: List[Dict]) -> Dict:
    """
    指标 2: Hypothesis Lifecycle Completion
    
    检查：
    - hypothesis 存在
    - closed 状态
    - conclusion 存在
    - status (success/failure/unknown)
    """
    total = len(experiments)
    has_hypothesis = 0
    is_closed = 0
    has_conclusion = 0
    status_dist = defaultdict(int)
    
    for exp in experiments:
        if exp.get("hypothesis"):
            has_hypothesis += 1
        
        if exp.get("closed"):
            is_closed += 1
        
        if exp.get("conclusion"):
            has_conclusion += 1
        
        status = exp.get("status", "unknown")
        status_dist[status] += 1
    
    return {
        "total": total,
        "has_hypothesis": has_hypothesis,
        "is_closed": is_closed,
        "has_conclusion": has_conclusion,
        "hypothesis_coverage": has_hypothesis / total if total > 0 else 0,
        "closure_rate": is_closed / total if total > 0 else 0,
        "conclusion_rate": has_conclusion / total if total > 0 else 0,
        "status_distribution": dict(status_dist),
    }


def validate_experiment_memory_transfer(
    experiments: List[Dict], memory: Dict
) -> Dict:
    """
    指标 3: Experiment -> Memory 转化率
    
    检查：
    - 成功实验的 hypothesis 是否出现在 memory 的 objects/unknowns/constraints 中
    - 使用关键词匹配（不强制精确匹配）
    """
    success_experiments = [
        exp for exp in experiments 
        if exp.get("status") == "success" and exp.get("hypothesis")
    ]
    
    if not success_experiments:
        return {
            "total_success": 0,
            "transferred": 0,
            "transfer_rate": 0.0,
            "missing": []
        }
    
    # 提取 memory 中所有文本（objects, unknowns, constraints）
    # ParsedLine 使用 .raw 属性访问原始文本
    memory_lines = []
    for key in ["objects", "unknowns", "constraints"]:
        for parsed_line in memory.get(key, []):
            if hasattr(parsed_line, 'raw'):
                memory_lines.append(parsed_line.raw)
            else:
                memory_lines.append(str(parsed_line))
    
    memory_text = " ".join(memory_lines).lower()
    
    transferred = 0
    missing = []
    
    for exp in success_experiments:
        hyp = exp["hypothesis"].lower()
        # 提取关键词（去除常见停用词）
        keywords = [w for w in hyp.split() if len(w) > 3 and w not in {
            "this", "that", "with", "from", "have", "been", "were", "their", "there"
        }]
        
        # 如果至少 3 个关键词在 memory 中，认为已转化
        if len(keywords) >= 3:
            matches = sum(1 for kw in keywords if kw in memory_text)
            if matches >= 3:
                transferred += 1
            else:
                missing.append({
                    "exp_id": exp.get("id", "unknown"),
                    "hypothesis": exp["hypothesis"][:80],
                    "matched_keywords": matches
                })
        else:
            # 关键词太少，用原始匹配
            if any(word in memory_text for word in hyp.split()[:5]):
                transferred += 1
            else:
                missing.append({
                    "exp_id": exp.get("id", "unknown"),
                    "hypothesis": exp["hypothesis"][:80],
                    "matched_keywords": 0
                })
    
    return {
        "total_success": len(success_experiments),
        "transferred": transferred,
        "transfer_rate": transferred / len(success_experiments) if success_experiments else 0.0,
        "missing": missing
    }


def validate_target(target: str) -> Dict:
    """验证单个目标的认知持久化"""
    experiments = load_experiments(target)
    memory = load_target_memory(target)
    
    if not experiments:
        return {"target": target, "status": "no_experiments", "experiment_count": 0}
    
    # 指标 1: Evidence Coverage
    evidence_metrics = validate_evidence_coverage(experiments)
    
    # 指标 2: Hypothesis Lifecycle
    lifecycle_metrics = validate_hypothesis_lifecycle(experiments)
    
    # 指标 3: Experiment -> Memory Transfer
    transfer_metrics = validate_experiment_memory_transfer(experiments, memory)
    
    return {
        "target": target,
        "status": "ok",
        "experiment_count": len(experiments),
        "evidence_coverage": evidence_metrics,
        "hypothesis_lifecycle": lifecycle_metrics,
        "experiment_memory_transfer": transfer_metrics,
    }


def print_result(result: Dict):
    """打印单个目标的验证结果"""
    print(f"\n[Evidence Coverage]")
    print(f"  evidence.md 覆盖率: {result['evidence_coverage']['evidence_coverage']:.1%}")
    print(f"  observations 覆盖率: {result['evidence_coverage']['observation_coverage']:.1%}")
    print(f"  artifacts 覆盖率: {result['evidence_coverage']['artifact_coverage']:.1%}")
    
    print(f"\n[Hypothesis Lifecycle]")
    print(f"  hypothesis 覆盖率: {result['hypothesis_lifecycle']['hypothesis_coverage']:.1%}")
    print(f"  关闭率: {result['hypothesis_lifecycle']['closure_rate']:.1%}")
    print(f"  结论率: {result['hypothesis_lifecycle']['conclusion_rate']:.1%}")
    print(f"  状态分布: {result['hypothesis_lifecycle']['status_distribution']}")
    
    print(f"\n[Experiment -> Memory 转化]")
    transfer = result['experiment_memory_transfer']
    print(f"  成功实验数: {transfer['total_success']}")
    print(f"  已转化数: {transfer['transferred']}")
    print(f"  转化率: {transfer['transfer_rate']:.1%}")
    if transfer['missing']:
        print(f"  未转化实验 ({len(transfer['missing'])}):")
        for miss in transfer['missing'][:3]:  # 只显示前 3 个
            print(f"    - {miss['exp_id']}: {miss['hypothesis'][:60]}... (匹配关键词: {miss['matched_keywords']})")



def main():
    """验证所有目标的认知持久化"""
    import argparse
    
    # 声明全局变量
    global XIANGXIONG_ROOT, RESULTS_DIR
    
    parser = argparse.ArgumentParser(description="验证 XiangXiong 认知持久化")
    parser.add_argument(
        "--xiangxiong-root",
        type=Path,
        help="XiangXiong 根目录路径（包含 experiments/ 和 results/ 的目录）",
    )
    args = parser.parse_args()
    
    # 使用命令行参数或默认路径
    xiangxiong_root = args.xiangxiong_root or XIANGXIONG_ROOT
    xiangxiong_root = Path(xiangxiong_root)
    
    print("XiangXiong v0.3 认知持久化验证")
    print(f"XiangXiong 根目录: {xiangxiong_root}")
    
    # 更新全局变量
    XIANGXIONG_ROOT = xiangxiong_root
    RESULTS_DIR = xiangxiong_root / "results"
    
    # 扫描所有目标（支持新旧两种目录结构）
    targets = set()
    
    # 新版：experiments/<target>/
    exp_root_new = xiangxiong_root / "experiments"
    if exp_root_new.exists():
        targets.update(d.name for d in exp_root_new.iterdir() if d.is_dir())
    
    # 旧版：results/<target>/experiments/
    results_root = xiangxiong_root / "results"
    if results_root.exists():
        for target_dir in results_root.iterdir():
            if target_dir.is_dir() and (target_dir / "experiments").exists():
                targets.add(target_dir.name)
    
    if not targets:
        print("❌ 未找到任何目标")
        print(f"   检查了：{exp_root_new} 和 {results_root}")
        return
    
    targets = sorted(targets)
    
    print(f"📁 发现 {len(targets)} 个目标: {', '.join(targets)}")
    print()
    
    # 汇总统计
    total_experiments = 0
    total_targets_with_exp = 0
    all_results = []
    
    for target in targets:
        print("=" * 60)
        print(f"目标: {target}")
        print("=" * 60)
        
        try:
            result = validate_target(target)
            
            if result["experiment_count"] == 0:
                print("⚠️  无实验数据")
            else:
                total_targets_with_exp += 1
                total_experiments += result["experiment_count"]
                all_results.append(result)
                
                print(f"📊 实验数量: {result['experiment_count']}")
                print()
                print_result(result)
            print()
        except Exception as e:
            print(f"❌ 验证失败: {e}")
            import traceback
            traceback.print_exc()
            print()
    
    # 输出汇总
    if all_results:
        print()
        print("=" * 60)
        print("📊 汇总统计")
        print("=" * 60)
        print(f"总目标数: {len(targets)}")
        print(f"有实验的目标: {total_targets_with_exp}")
        print(f"总实验数: {total_experiments}")
        print()
        
        # 计算平均指标
        avg_evidence_coverage = sum(r["evidence_coverage"]["evidence_coverage"] for r in all_results) / len(all_results)
        avg_closure_rate = sum(r["hypothesis_lifecycle"]["closure_rate"] for r in all_results) / len(all_results)
        avg_conclusion_rate = sum(r["hypothesis_lifecycle"]["conclusion_rate"] for r in all_results) / len(all_results)
        
        print(f"平均 evidence.md 覆盖率: {avg_evidence_coverage:.1%}")
        print(f"平均实验关闭率: {avg_closure_rate:.1%}")
        print(f"平均结论率: {avg_conclusion_rate:.1%}")
        print()
        
        # 状态分布汇总
        all_statuses = {}
        for r in all_results:
            for status, count in r["hypothesis_lifecycle"]["status_distribution"].items():
                all_statuses[status] = all_statuses.get(status, 0) + count
        print(f"实验状态分布: {all_statuses}")
        print()
        
        # 转化率统计
        total_success_exps = sum(r["experiment_memory_transfer"]["total_success"] for r in all_results)
        total_transferred = sum(r["experiment_memory_transfer"]["transferred"] for r in all_results)
        
        if total_success_exps > 0:
            overall_transfer_rate = total_transferred / total_success_exps
            print(f"成功实验总数: {total_success_exps}")
            print(f"已转化到 Memory: {total_transferred}")
            print(f"总体转化率: {overall_transfer_rate:.1%}")
        
        # 最终评价
        print()
        print("=" * 60)
        if avg_evidence_coverage >= 0.8 and avg_closure_rate >= 0.9 and avg_conclusion_rate >= 0.9:
            print("✅ XiangXiong 认知持久化能力: 优秀")
        elif avg_evidence_coverage >= 0.7 and avg_closure_rate >= 0.8:
            print("✅ XiangXiong 认知持久化能力: 合格")
        else:
            print("⚠️  XiangXiong 认知持久化能力: 需改进")
            if avg_evidence_coverage < 0.7:
                print("  - Evidence Coverage 不足 70%")
            if avg_closure_rate < 0.8:
                print("  - 实验关闭率不足 80%")
    else:
        print()
        print("⚠️  未找到任何有效的实验数据")


if __name__ == "__main__":
    main()
