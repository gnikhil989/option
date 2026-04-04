"""
Daily Trade Book - Consolidates all strategy CSVs into a single daily Excel file.

Produces: logs/v10_tradebook_YYYY-MM-DD.xlsx
Sheets: One per strategy + "Summary" sheet with aggregated metrics.

Called every 60 seconds from the trade engine loop.
Reads from individual strategy CSVs (source of truth) and writes Excel.
"""

import os
import csv
import logging
import threading
from datetime import datetime
from collections import defaultdict

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    # Try to install automatically
    try:
        import subprocess, sys
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'openpyxl', '-q'])
        from openpyxl import Workbook, load_workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        OPENPYXL_AVAILABLE = True
    except Exception:
        OPENPYXL_AVAILABLE = False

logger = logging.getLogger(__name__)


class DailyTradeBook:
    """Consolidates multiple strategy CSVs into a single daily Excel workbook."""

    def __init__(self, log_dir="logs"):
        self.log_dir = log_dir
        self.lock = threading.Lock()
        self._last_consolidation = 0
        self.consolidation_interval = 60  # seconds

        # Style definitions
        if OPENPYXL_AVAILABLE:
            self._header_font = Font(bold=True, color="FFFFFF", size=10)
            self._header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
            self._entry_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
            self._exit_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
            self._profit_font = Font(color="006100", bold=True)
            self._loss_font = Font(color="9C0006", bold=True)
            self._border = Border(
                bottom=Side(style='thin', color='D9D9D9')
            )

    @property
    def today_filename(self):
        """Excel stays in logs/ root (NOT in date folder) for easy access."""
        date_str = datetime.now().strftime("%d-%m-%Y")
        return os.path.join(self.log_dir, f"v10_tradebook_{date_str}.xlsx")

    def should_consolidate(self):
        """Check if enough time has passed since last consolidation."""
        import time
        return (time.time() - self._last_consolidation) >= self.consolidation_interval

    def consolidate(self, strategies):
        """Main entry point: read all strategy CSVs, write single Excel.

        Args:
            strategies: list of strategy objects (each has .name and .logger.filename)
        """
        if not self.should_consolidate():
            return

        if not OPENPYXL_AVAILABLE:
            # Warn only once, not every second
            if not getattr(self, '_openpyxl_warned', False):
                logger.warning("[TRADEBOOK] openpyxl not installed. Install with: pip install openpyxl. Excel consolidation disabled.")
                self._openpyxl_warned = True
            return

        import time

        import time
        with self.lock:
            try:
                self._do_consolidation(strategies)
            except Exception as e:
                logger.error(f"[TRADEBOOK] Consolidation error: {e}")
            finally:
                # Always update timestamp to prevent retry-spam on error
                self._last_consolidation = time.time()

    def _do_consolidation(self, strategies):
        """Read CSVs and write Excel."""
        wb = Workbook()

        # Remove default sheet
        if 'Sheet' in wb.sheetnames:
            del wb['Sheet']

        today_str = datetime.now().strftime("%Y-%m-%d")
        all_trades = []
        strategy_summaries = {}

        for strategy in strategies:
            csv_path = strategy.logger.filename
            name = strategy.name

            # Read today's trades from CSV
            trades = self._read_todays_trades(csv_path, today_str)

            # Create sheet for this strategy
            ws = wb.create_sheet(title=name[:31])  # Excel sheet name max 31 chars
            self._write_strategy_sheet(ws, name, trades)

            # Collect for summary
            all_trades.extend(trades)
            strategy_summaries[name] = self._calc_strategy_summary(trades)

        # Create Summary sheet (first position)
        summary_ws = wb.create_sheet(title="Summary", index=0)
        self._write_summary_sheet(summary_ws, strategy_summaries, today_str)

        # Create All Trades sheet
        all_ws = wb.create_sheet(title="All Trades", index=1)
        self._write_strategy_sheet(all_ws, "All Strategies", all_trades)

        # Save
        os.makedirs(self.log_dir, exist_ok=True)
        wb.save(self.today_filename)
        logger.debug(f"[TRADEBOOK] Saved {self.today_filename} ({len(all_trades)} trades across {len(strategies)} strategies)")

    def _read_todays_trades(self, csv_path, today_str):
        """Read all trades from today's dated CSV file.
        Since CSVs are now in date folders (logs/DD-MM-YYYY/), all rows are today's trades.
        """
        trades = []
        if not os.path.exists(csv_path):
            return trades

        try:
            with open(csv_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trades.append(row)
        except Exception as e:
            logger.error(f"[TRADEBOOK] Error reading {csv_path}: {e}")

        return trades

    def _write_strategy_sheet(self, ws, title, trades):
        """Write trades to an Excel sheet with formatting."""
        if not trades:
            ws.append(["No trades recorded today"])
            return

        # Headers from first trade's keys
        headers = list(trades[0].keys())
        ws.append(headers)

        # Style headers
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = self._header_font
            cell.fill = self._header_fill
            cell.alignment = Alignment(horizontal='center')

        # Data rows
        for row_idx, trade in enumerate(trades, 2):
            for col_idx, header in enumerate(headers, 1):
                value = trade.get(header, '')

                # Ensure value is a primitive type (str, int, float) for Excel
                if isinstance(value, (list, dict, tuple, set)):
                    value = str(value)

                # Try to convert numeric string values
                try:
                    str_val = str(value).strip()
                    if str_val and '.' in str_val and str_val.replace('.', '', 1).replace('-', '', 1).isdigit():
                        value = float(str_val)
                    elif str_val and str_val.lstrip('-').isdigit():
                        value = int(str_val)
                except (ValueError, TypeError):
                    pass

                # Final safety: if still not a valid Excel type, convert to string
                if not isinstance(value, (str, int, float, bool, type(None))):
                    value = str(value)

                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.border = self._border

            # Row coloring based on Status
            status = trade.get('Status', '')
            fill = self._entry_fill if status == 'ENTRY' else self._exit_fill if status == 'EXIT' else None
            if fill:
                for col_idx in range(1, len(headers) + 1):
                    ws.cell(row=row_idx, column=col_idx).fill = fill

            # PnL coloring
            pnl_col = None
            for idx, h in enumerate(headers, 1):
                if h == 'PnL_Amount':
                    pnl_col = idx
                    break
            if pnl_col and status == 'EXIT':
                pnl_cell = ws.cell(row=row_idx, column=pnl_col)
                try:
                    pnl_val = float(trade.get('PnL_Amount', 0) or 0)
                    pnl_cell.font = self._profit_font if pnl_val > 0 else self._loss_font
                except (ValueError, TypeError):
                    pass

        # Auto-width columns
        for col_idx, header in enumerate(headers, 1):
            max_width = max(len(str(header)), 10)
            for row in ws.iter_rows(min_col=col_idx, max_col=col_idx, min_row=2, max_row=min(len(trades) + 1, 20)):
                for cell in row:
                    if cell.value:
                        max_width = max(max_width, len(str(cell.value)))
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max_width + 2, 30)

        # Freeze header row
        ws.freeze_panes = 'A2'

    def _calc_strategy_summary(self, trades):
        """Calculate summary metrics for a strategy's trades."""
        exits = [t for t in trades if t.get('Status') == 'EXIT']

        if not exits:
            return {
                'total_trades': 0,
                'wins': 0,
                'losses': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'avg_pnl': 0,
                'best_trade': 0,
                'worst_trade': 0,
                'avg_hold_time': 0,
            }

        pnls = []
        hold_times = []
        for t in exits:
            try:
                pnls.append(float(t.get('PnL_Amount', 0) or 0))
            except (ValueError, TypeError):
                pnls.append(0)
            try:
                hold_times.append(int(t.get('Time_Held_Sec', 0) or 0))
            except (ValueError, TypeError):
                hold_times.append(0)

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        return {
            'total_trades': len(exits),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(len(wins) / len(exits) * 100, 1) if exits else 0,
            'total_pnl': round(sum(pnls), 2),
            'avg_pnl': round(sum(pnls) / len(pnls), 2) if pnls else 0,
            'best_trade': round(max(pnls), 2) if pnls else 0,
            'worst_trade': round(min(pnls), 2) if pnls else 0,
            'avg_hold_time': round(sum(hold_times) / len(hold_times) / 60, 1) if hold_times else 0,
        }

    def _write_summary_sheet(self, ws, strategy_summaries, today_str):
        """Write the Summary sheet with aggregated metrics per strategy."""
        # Title
        ws.merge_cells('A1:I1')
        title_cell = ws.cell(row=1, column=1, value=f"V10 Trade Book - {today_str}")
        title_cell.font = Font(bold=True, size=14, color="2F5496")
        title_cell.alignment = Alignment(horizontal='center')

        # Headers
        headers = ['Strategy', 'Trades', 'Wins', 'Losses', 'Win Rate %', 'Total PnL (₹)',
                   'Avg PnL (₹)', 'Best Trade (₹)', 'Worst Trade (₹)', 'Avg Hold (min)']
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=3, column=col_idx, value=header)
            cell.font = self._header_font
            cell.fill = self._header_fill
            cell.alignment = Alignment(horizontal='center')

        # Per-strategy rows
        row = 4
        total_trades = 0
        total_wins = 0
        total_losses = 0
        total_pnl = 0

        for name, summary in strategy_summaries.items():
            ws.cell(row=row, column=1, value=name).font = Font(bold=True)
            ws.cell(row=row, column=2, value=summary['total_trades'])
            ws.cell(row=row, column=3, value=summary['wins'])
            ws.cell(row=row, column=4, value=summary['losses'])
            ws.cell(row=row, column=5, value=summary['win_rate'])
            pnl_cell = ws.cell(row=row, column=6, value=summary['total_pnl'])
            pnl_cell.font = self._profit_font if summary['total_pnl'] >= 0 else self._loss_font
            ws.cell(row=row, column=7, value=summary['avg_pnl'])
            ws.cell(row=row, column=8, value=summary['best_trade'])
            ws.cell(row=row, column=9, value=summary['worst_trade'])
            ws.cell(row=row, column=10, value=summary['avg_hold_time'])

            for col in range(1, 11):
                ws.cell(row=row, column=col).border = self._border

            total_trades += summary['total_trades']
            total_wins += summary['wins']
            total_losses += summary['losses']
            total_pnl += summary['total_pnl']
            row += 1

        # Totals row
        row += 1
        total_fill = PatternFill(start_color="D6DCE4", end_color="D6DCE4", fill_type="solid")
        ws.cell(row=row, column=1, value="TOTAL").font = Font(bold=True, size=11)
        ws.cell(row=row, column=2, value=total_trades).font = Font(bold=True)
        ws.cell(row=row, column=3, value=total_wins).font = Font(bold=True)
        ws.cell(row=row, column=4, value=total_losses).font = Font(bold=True)
        wr = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0
        ws.cell(row=row, column=5, value=wr).font = Font(bold=True)
        pnl_total_cell = ws.cell(row=row, column=6, value=round(total_pnl, 2))
        pnl_total_cell.font = Font(bold=True, size=12, color="006100" if total_pnl >= 0 else "9C0006")

        for col in range(1, 11):
            ws.cell(row=row, column=col).fill = total_fill

        # Auto-width
        for col_idx in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = 16

        ws.freeze_panes = 'A4'
