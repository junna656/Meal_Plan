# Mahlzeit-Planer für Home Assistant

Eine Home-Assistant-App für die Familie: Abendessen der Woche planen, Rezepte sammeln, Wünsche abgeben und die Einkaufsliste automatisch erstellen.

- **Woche:** pro Abend ein Gericht. Wer mitisst, kommt aus festen Mustern (z. B. Kinder nur in geraden Kalenderwochen) und lässt sich pro Abend antippen.
- **Vegetarisch:** Wer vegetarisch isst, bekommt automatisch die Veggie-Variante; Fleischgerichte ohne Variante werden markiert.
- **Einkauf:** Zutaten werden über alle Abende zusammengezählt, Mengen nach den Personen am Tisch. Die Liste für den Samstagseinkauf deckt Sa und So der laufenden Woche plus Mo bis Mi der neuen Planwoche ab (Do und Fr kauft ihr frisch). Abhaken, Extras ergänzen, mit einem Tipp in eine To-do-Liste von Home Assistant übernehmen.
- **Wünsche:** Jeder kann sich Gerichte wünschen; Wünsche kommen bei Vorschlägen zuerst dran.
- **KI (optional):** Mit einer `ai_task`-Entität gibt es Rezept-Ideen und Rezepte zu Wünschen.

Alle Daten bleiben bei dir in Home Assistant (`/data/store.json` der App, in Backups enthalten).

## Installation

1. Einstellungen → Apps → App-Store → ⋮ → Repositories → `https://github.com/junna656/meal_plan` hinzufügen.
2. „Mahlzeit-Planer“ installieren und starten, „In Seitenleiste anzeigen“ einschalten.
3. Im Reiter **Konfiguration** die Personen eintragen.

## Muster

`muster` sagt, wann jemand mitisst. Mehrere Regeln mit `;` trennen:

| Muster | Bedeutung |
|---|---|
| `immer` | jeden Tag |
| `gerade` / `ungerade` | in geraden / ungeraden Kalenderwochen |
| `immer di,do` | jeden Dienstag und Donnerstag |
| `immer di,do; gerade fr,sa,so` | Di und Do, in geraden Wochen auch Fr–So |
| leer oder `nie` | nur von Hand |

## Rezepte importieren

Unter **Wünsche → Rezepte als Text importieren** lassen sich Rezepte als JSON einfügen:

```json
{"name": "Linsen-Dal", "tags": ["schnell"], "steps": "…",
 "ing": [{"n": "Rote Linsen", "q": 80, "u": "g", "t": "all"}]}
```

Mengen gelten pro Erwachsenenportion. `u`: g, ml, Stk, EL, TL, Pck, Bund. `t`: `all` (für alle), `meat` (Fleisch, nur Fleischesser), `veg` (vegetarischer Ersatz).
