#!/usr/bin/env python3
"""Tiny local parity viewer for paper/live normalized audit rows."""

from __future__ import annotations

import argparse
import html
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.parity_audit import build_parity_view, write_parity_comparison_artifact

DATA_DIR = ROOT / "data"
HOST = "127.0.0.1"
PORT = 8765


def _render_summary(title: str, summary: dict) -> str:
    items = [
        f"<li><b>Total rows:</b> {summary.get('total_rows', 0)}</li>",
        f"<li><b>Parity candidates:</b> {summary.get('parity_candidates', 0)}</li>",
        f"<li><b>Status counts:</b> <code>{html.escape(json.dumps(summary.get('status_counts', {}), sort_keys=True))}</code></li>",
        f"<li><b>Lifecycle states:</b> <code>{html.escape(json.dumps(summary.get('lifecycle_state_counts', {}), sort_keys=True))}</code></li>",
        f"<li><b>Failure stages:</b> <code>{html.escape(json.dumps(summary.get('failure_stage_counts', {}), sort_keys=True))}</code></li>",
        f"<li><b>Revalidation outcomes:</b> <code>{html.escape(json.dumps(summary.get('execution_revalidation_outcome_counts', {}), sort_keys=True))}</code></li>",
        f"<li><b>Snapshot sources:</b> <code>{html.escape(json.dumps(summary.get('snapshot_source_counts', {}), sort_keys=True))}</code></li>",
        f"<li><b>Schema-gap rows:</b> {summary.get('schema_gap_rows', 0)}</li>",
        f"<li><b>Top schema gaps:</b> <code>{html.escape(json.dumps(summary.get('top_schema_gaps', [])))}</code></li>",
        f"<li><b>Schema-gap examples:</b> <code>{html.escape(json.dumps(summary.get('schema_gap_examples', [])))}</code></li>",
        f"<li><b>Lifecycle contradiction rows:</b> {summary.get('lifecycle_contradiction_rows', 0)}</li>",
        f"<li><b>Top lifecycle contradictions:</b> <code>{html.escape(json.dumps(summary.get('top_lifecycle_contradictions', [])))}</code></li>",
        f"<li><b>Lifecycle contradiction examples:</b> <code>{html.escape(json.dumps(summary.get('lifecycle_contradiction_examples', [])))}</code></li>",
        f"<li><b>Invalid contract rows:</b> {summary.get('invalid_contract_rows', 0)}</li>",
        f"<li><b>Decision delta rows:</b> {summary.get('decision_delta_rows', 0)}</li>",
        f"<li><b>Decision delta pairs:</b> <code>{html.escape(json.dumps(summary.get('top_decision_delta_pairs', [])))}</code></li>",
        f"<li><b>Decision delta examples:</b> <code>{html.escape(json.dumps(summary.get('decision_delta_examples', [])))}</code></li>",
        f"<li><b>Execution price delta rows:</b> {summary.get('execution_price_delta_rows', 0)}</li>",
        f"<li><b>Price delta examples:</b> <code>{html.escape(json.dumps(summary.get('price_delta_examples', [])))}</code></li>",
        f"<li><b>Resolved outcomes:</b> <code>{html.escape(json.dumps(summary.get('resolved_outcome_counts', {}), sort_keys=True))}</code></li>",
        f"<li><b>Top contract issues:</b> <code>{html.escape(json.dumps(summary.get('top_contract_issues', [])))}</code></li>",
        f"<li><b>Invalid examples:</b> <code>{html.escape(json.dumps(summary.get('invalid_contract_examples', [])))}</code></li>",
        f"<li><b>Top reasons:</b> <code>{html.escape(json.dumps(summary.get('top_reason_codes', [])))}</code></li>",
    ]
    return f"<section><h2>{html.escape(title)}</h2><ul>{''.join(items)}</ul></section>"


def _render_comparison(view: dict) -> str:
    comparison = view.get("comparison", {})
    items = [
        f"<li><b>Artifact path:</b> <code>{html.escape(str(view.get('comparison_artifact_path') or ''))}</code> (<a href=\"/export\">write</a>)</li>",
        f"<li><b>Paper rows:</b> {comparison.get('paper_rows', 0)}</li>",
        f"<li><b>Live rows:</b> {comparison.get('live_rows', 0)}</li>",
        f"<li><b>Matched keys:</b> {comparison.get('matched_keys', 0)}</li>",
        f"<li><b>Matched pairs:</b> {comparison.get('matched_pairs', 0)}</li>",
        f"<li><b>Paper-only rows:</b> {comparison.get('paper_only_row_count', 0)}</li>",
        f"<li><b>Live-only rows:</b> {comparison.get('live_only_row_count', 0)}</li>",
        f"<li><b>Mismatched pairs:</b> {comparison.get('mismatched_pair_count', 0)}</li>",
        f"<li><b>Mismatch fields:</b> <code>{html.escape(json.dumps(comparison.get('mismatch_field_counts', [])))}</code></li>",
        f"<li><b>Mismatch examples:</b> <code>{html.escape(json.dumps(comparison.get('mismatch_examples', [])))}</code></li>",
        f"<li><b>Paper-only keys:</b> <code>{html.escape(json.dumps(comparison.get('paper_only_keys', [])))}</code></li>",
        f"<li><b>Live-only keys:</b> <code>{html.escape(json.dumps(comparison.get('live_only_keys', [])))}</code></li>",
    ]
    return f"<section><h2>Paper/live comparison</h2><ul>{''.join(items)}</ul></section>"


def _render_rows(title: str, rows: list[dict]) -> str:
    head = """
    <tr>
      <th>timestamp</th><th>market_id</th><th>status</th><th>lifecycle</th><th>reason_code</th>
      <th>original_code</th><th>execution_code</th><th>price_delta</th>
      <th>requested</th><th>filled</th><th>entry</th><th>parity</th><th>snapshot_source</th>
      <th>schema_gaps</th><th>lifecycle_flags</th><th>issues</th>
    </tr>
    """
    body = []
    for row in rows[-50:][::-1]:
        body.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('timestamp') or ''))}</td>"
            f"<td>{html.escape(str(row.get('market_id') or ''))}</td>"
            f"<td>{html.escape(str(row.get('status') or ''))}</td>"
            f"<td>{html.escape(str(row.get('lifecycle_state') or ''))}</td>"
            f"<td>{html.escape(str(row.get('decision_reason_code') or ''))}</td>"
            f"<td>{html.escape(str(row.get('original_decision_reason_code') or ''))}</td>"
            f"<td>{html.escape(str(row.get('execution_decision_reason_code') or ''))}</td>"
            f"<td>{html.escape(str(row.get('execution_market_price_delta') or ''))}</td>"
            f"<td>{html.escape(str(row.get('requested_size') or ''))}</td>"
            f"<td>{html.escape(str(row.get('filled_size') or ''))}</td>"
            f"<td>{html.escape(str(row.get('entry_price') or ''))}</td>"
            f"<td>{'yes' if row.get('is_parity_candidate') else 'no'}</td>"
            f"<td>{html.escape(str(row.get('execution_snapshot_source') or ''))}</td>"
            f"<td>{html.escape(', '.join(row.get('schema_gaps') or []))}</td>"
            f"<td>{html.escape(', '.join(row.get('lifecycle_contradictions') or []))}</td>"
            f"<td>{html.escape(', '.join(row.get('contract_issues') or []))}</td>"
            "</tr>"
        )
    return f"<section><h2>{html.escape(title)}</h2><table>{head}{''.join(body)}</table></section>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        view = build_parity_view(DATA_DIR)
        if parsed.path == "/data":
            payload = json.dumps(view, indent=2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode())
            return
        if parsed.path == "/comparison":
            payload = json.dumps(view.get("comparison", {}), indent=2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode())
            return
        if parsed.path == "/export":
            path = write_parity_comparison_artifact(DATA_DIR)
            payload = json.dumps({"written": str(path)}, indent=2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode())
            return

        page = f"""
        <html>
        <head>
          <meta charset=\"utf-8\" />
          <title>Prediction Bot Parity Viewer</title>
          <style>
            body {{ font-family: sans-serif; margin: 24px; background: #111; color: #eee; }}
            code {{ color: #9fe870; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
            th, td {{ border: 1px solid #444; padding: 6px 8px; font-size: 12px; text-align: left; }}
            th {{ background: #222; position: sticky; top: 0; }}
            section {{ margin-bottom: 24px; }}
            a {{ color: #7cc7ff; }}
          </style>
        </head>
        <body>
          <h1>Prediction Bot Parity Viewer</h1>
          <p>Normalized paper/live audit view. JSON: <a href=\"/data\">/data</a>; comparison: <a href=\"/comparison\">/comparison</a></p>
          {_render_summary('Paper summary', view['paper_summary'])}
          {_render_summary('Live summary', view['live_summary'])}
          {_render_comparison(view)}
          {_render_rows('Paper rows (latest 50)', view['paper_rows'])}
          {_render_rows('Live rows (latest 50)', view['live_rows'])}
        </body>
        </html>
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())


def main() -> None:
    global DATA_DIR, HOST, PORT

    parser = argparse.ArgumentParser(description="Inspect normalized paper/live parity audit rows.")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="Data directory containing paper/live audit files.")
    parser.add_argument("--host", default=HOST, help="HTTP bind host.")
    parser.add_argument("--port", type=int, default=PORT, help="HTTP bind port.")
    parser.add_argument(
        "--export",
        nargs="?",
        const="",
        help="Write the comparison artifact and exit. Optionally provide an output JSON path.",
    )
    args = parser.parse_args()

    DATA_DIR = Path(args.data_dir)
    HOST = args.host
    PORT = args.port
    if args.export is not None:
        path = write_parity_comparison_artifact(DATA_DIR, output_path=args.export or None)
        print(f"Wrote parity comparison artifact to {path}")
        return

    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"Parity viewer listening on http://{HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
