#!/usr/bin/env python3
"""Tiny local parity viewer for paper/live normalized audit rows."""

from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from bot.parity_audit import build_parity_view

ROOT = Path(__file__).resolve().parent.parent
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
        f"<li><b>Invalid contract rows:</b> {summary.get('invalid_contract_rows', 0)}</li>",
        f"<li><b>Decision delta rows:</b> {summary.get('decision_delta_rows', 0)}</li>",
        f"<li><b>Execution price delta rows:</b> {summary.get('execution_price_delta_rows', 0)}</li>",
        f"<li><b>Resolved outcomes:</b> <code>{html.escape(json.dumps(summary.get('resolved_outcome_counts', {}), sort_keys=True))}</code></li>",
        f"<li><b>Top contract issues:</b> <code>{html.escape(json.dumps(summary.get('top_contract_issues', [])))}</code></li>",
        f"<li><b>Invalid examples:</b> <code>{html.escape(json.dumps(summary.get('invalid_contract_examples', [])))}</code></li>",
        f"<li><b>Top reasons:</b> <code>{html.escape(json.dumps(summary.get('top_reason_codes', [])))}</code></li>",
    ]
    return f"<section><h2>{html.escape(title)}</h2><ul>{''.join(items)}</ul></section>"


def _render_rows(title: str, rows: list[dict]) -> str:
    head = """
    <tr>
      <th>timestamp</th><th>market_id</th><th>status</th><th>lifecycle</th><th>reason_code</th>
      <th>requested</th><th>filled</th><th>entry</th><th>parity</th><th>snapshot_source</th><th>issues</th>
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
            f"<td>{html.escape(str(row.get('requested_size') or ''))}</td>"
            f"<td>{html.escape(str(row.get('filled_size') or ''))}</td>"
            f"<td>{html.escape(str(row.get('entry_price') or ''))}</td>"
            f"<td>{'yes' if row.get('is_parity_candidate') else 'no'}</td>"
            f"<td>{html.escape(str(row.get('execution_snapshot_source') or ''))}</td>"
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
          <p>Normalized paper/live audit view. JSON: <a href=\"/data\">/data</a></p>
          {_render_summary('Paper summary', view['paper_summary'])}
          {_render_summary('Live summary', view['live_summary'])}
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
    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"Parity viewer listening on http://{HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
