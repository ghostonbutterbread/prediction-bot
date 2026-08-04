#!/usr/bin/env python3
"""Materialize paper-only settled source-performance observations from a cohort."""
from __future__ import annotations
import argparse, json, sys
from dataclasses import asdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from bot.weather.source_performance_materializer import materialize_source_performance_once

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--cohort-id',required=True); p.add_argument('--snapshot-path',action='append',required=True)
 p.add_argument('--resolution-path',required=True); p.add_argument('--output-path',required=True); p.add_argument('--report-path',required=True)
 a=p.parse_args(); r=materialize_source_performance_once(cohort_id=a.cohort_id,snapshot_paths=a.snapshot_path,resolution_path=a.resolution_path,output_path=a.output_path,report_path=a.report_path)
 print(json.dumps(asdict(r),default=str,sort_keys=True))
if __name__=='__main__': main()
