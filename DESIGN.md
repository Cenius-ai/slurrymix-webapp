# Slurrymix design direction

Slurrymix is a **CLI**, so its surface is the terminal: dark by default, monospace,
dense, and honest about numbers. The committed direction below is the single source
of truth; the tokens live in the `THEME` mapping at the top of `slurrymix.py` and
every table, bar and heading in the tool reads from it. Nothing here is decoration —
each choice serves reading numbers off a screen at arm's length in a yard office.

## Tokens

| Token | Value | Where it is used |
| --- | --- | --- |
| `surface` | `terminal-dark` | The only surface: the operator's own terminal, no chrome, no boxes around the whole screen |
| `type` | system monospace | The terminal's own font; Slurrymix ships no font and downloads none |
| `accent` | `#008da0` = `oklch(0.58 0.12 209)` | ONE accent, for primary status only: headings, the filled part of a percent bar, the record name in a confirmation line |
| `bold` | ANSI bold | Column headers, `warning:` / `error:` labels |
| `dim` | ANSI dim | The box-drawing rule under a table header, hints |
| `reset` | ANSI reset | Ends every styled run |

Colour is a convenience, never a signal: `--no-color`, `NO_COLOR` and a non-TTY
stdout all produce the same information, because every value that a colour could
carry is also spelled out — percent bars print `[######----] 64%`, over-capacity
units print a `FLAG` column and a `warning:` line, and every number carries its unit.

## Layout

* **Aligned columns.** Header row, box-drawing rule (`─`), then one record per line.
  Numbers are right-aligned, names left-aligned, so a column of volumes compares at
  a glance.
* **Compact density.** One blank line between blocks, two spaces between columns, no
  decorative padding and no full-screen clearing — output stays in the scrollback and
  pipes cleanly.
* **Help-first.** `--help` lists every command with a one-line description and a
  block of copy-pasteable examples; `status` on an empty state prints the first-run
  checklist in the order the commands should be run.
* **Units on every number.** Wide tables carry the unit in the column header
  (`VOLUME (m3)`, `TARGET (kg N/ha)`); narrow tables and single-value lines carry it
  next to the value (`950 m3`, `4.2 kg N/m3`, `79% full`). Never a bare number.
* **No chrome.** No panels, no borders around the screen, no progress bars that do
  not measure anything, no spinners: every command is synchronous and returns in
  milliseconds.

## Voice

Error messages say what went wrong, name the flag or record, and how to fix it —
`error: dm must be between 0 and 100`, `hint: for example --dm 8.5`,
`no field named 'South 3'` followed by the available names. Warnings (a field over
its season budget, a store over capacity) go to stderr and never hide the number
that caused them: the record is still stored and the tool still exits 0.

## Palette contrast

The accent (`#008da0`) is used on the terminal's own dark background and is only
ever applied to text that is also bold, or paired with a numeric value, so contrast
never has to carry meaning on its own. Body text is the terminal's default
foreground (typically `#e6e6e6`-or-better on dark), which keeps the primary readout
above 4.5:1 on every surface Slurrymix prints to.
