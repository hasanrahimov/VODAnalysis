"""Pretty console output (Rich) of the analysis results for manual entry."""

from typing import List, Dict

from rich.console import Console
from rich.table import Table
from rich import box

# record=True lets us export everything that was printed into a .txt report
console = Console(record=True)


def save_report(path: str):
    """Save everything printed so far to a plain-text file."""
    console.save_text(path, clear=False)


def _fmt_kill(killer: str, victim: str, kind: str) -> str:
    """Color a kill: our kill = green killer, our death = red victim."""
    if kind == "our_kill":
        return f"[green]{killer}[/green] → {victim}"
    if kind == "our_death":
        return f"{killer} → [red]{victim}[/red]"
    return f"[dim]{killer} → {victim}[/dim]"


def print_frame_timeline(timeline: List[Dict]):
    """
    Per-frame log — the safety net. Each row is one analyzed frame.
    timeline item: {time, phase, timer, plant, operators, kills:[(killer,victim,matched)]}
    """
    table = Table(
        title="Frame Timeline",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("Time", style="dim", width=8)
    table.add_column("Phase", width=10)
    table.add_column("Timer", width=6)
    table.add_column("P", width=2, justify="center")   # plant
    table.add_column("Kills (killer → victim)", overflow="fold")

    for item in timeline:
        kills = item.get("kills", [])
        if kills:
            kill_strs = []
            for killer, victim, kind in kills:
                kill_strs.append(_fmt_kill(killer, victim, kind))
            kills_cell = "  •  ".join(kill_strs)
        else:
            kills_cell = "[dim]—[/dim]"

        phase = item.get("phase", "")
        phase_col = {
            "op_pick": "magenta",
            "action": "white",
            "round_end": "cyan",
        }.get(phase, "dim")

        table.add_row(
            item.get("time", ""),
            f"[{phase_col}]{phase}[/{phase_col}]",
            item.get("timer") or "",
            "✓" if item.get("plant") else "",
            kills_cell,
        )

    console.print(table)


def _fmt_plant(r: Dict) -> str:
    """Plant cell: confirmed (✓ + planter), interrupted (start only), or none."""
    if r.get("plant"):
        by = r.get("plant_by")
        return f"[green]✓[/green] {by}" if by else "[green]✓[/green]"
    if r.get("plant_started"):
        return "[yellow]start✗[/yellow]"
    return "[dim]—[/dim]"


def print_round_summary(rounds: List[Dict]):
    """
    Aggregated per-round view — ready for manual copy into Sheets.
    Shows our team's kills (per player) and deaths (per player ← killer).
    """
    table = Table(
        title="Round Summary (manual-entry view)",
        box=box.ROUNDED,
        header_style="bold green",
        show_lines=True,
    )
    table.add_column("#", width=3, justify="right")
    table.add_column("Side", width=4)
    table.add_column("Score", width=6)
    table.add_column("Res", width=5)
    table.add_column("OK/OD", width=9)
    table.add_column("Plant", width=10)
    table.add_column("Our Kills", overflow="fold")
    table.add_column("Our Deaths (← killer / trade)", overflow="fold")
    table.add_column("KOST", overflow="fold")

    for r in rounds:
        result = r.get("result", "?")
        res_col = "green" if result == "WIN" else "red" if result == "LOSS" else "yellow"

        # OK/OD
        okod = r.get("ok_od")
        who = r.get("ok_od_who")
        if okod == "OK":
            okod_cell = f"[green]OK[/green] {who or ''}"
        elif okod == "OD":
            okod_cell = f"[red]OD[/red] {who or ''}"
        else:
            okod_cell = "[dim]—[/dim]"

        # Our kills: "MrBom ×2, Tora ×1"
        ok = r.get("our_kills", {})
        kills_cell = (
            ", ".join(f"[green]{name}[/green] ×{cnt}" for name, cnt in ok.items())
            if ok else "[dim]—[/dim]"
        )

        # Our deaths with trade info: "Hope ← drayn.s7  [traded by Tora]"
        od = r.get("our_deaths", {})
        traded = r.get("traded", {})
        if od:
            lines = []
            for name, killer in od.items():
                line = f"[red]{name}[/red] ← {killer}"
                if name in traded:
                    line += f"  [cyan][traded by {traded[name]}][/cyan]"
                lines.append(line)
            deaths_cell = "\n".join(lines)
        else:
            deaths_cell = "[dim]—[/dim]"

        score = r.get("score")
        score_cell = f"{score[0]}-{score[1]}" if score else "[dim]—[/dim]"

        # KOST per player: green 1 / red 0
        kost = r.get("kost", {})
        kost_cell = ("  ".join(
            f"{n}:[green]1[/green]" if v else f"{n}:[red]0[/red]"
            for n, v in kost.items()
        ) if kost else "[dim]—[/dim]")

        table.add_row(
            str(r.get("round", "?")),
            r.get("side", "?"),
            score_cell,
            f"[{res_col}]{result}[/{res_col}]",
            okod_cell,
            _fmt_plant(r),
            kills_cell,
            deaths_cell,
            kost_cell,
        )

    console.print(table)


def print_warnings(warnings: List[str]):
    if not warnings:
        return
    console.print("\n[bold yellow]⚠  Warnings / low-confidence items:[/bold yellow]")
    for w in warnings:
        console.print(f"   [yellow]•[/yellow] {w}")
