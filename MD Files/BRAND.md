# Brand and design system

## Name
Klasser AI

## Tagline
Timetabling, handled.

---

## Colours

```css
:root {
  /* Primary */
  --purple:         #6366F1;   /* Main brand colour — buttons, nav active, accents */
  --purple-dark:    #4F46E5;   /* Hover states, text on light backgrounds */
  --purple-light:   #EEF0FF;   /* Backgrounds, chips, tags */
  --purple-border:  #D8DAFF;   /* Borders on purple-light backgrounds */

  /* Accent */
  --amber:          #F5A623;   /* Active nav item, hover states, CTA */
  --amber-dark:     #E09010;   /* Amber hover */
  --amber-light:    #FFF8EC;   /* Amber background tint */

  /* Text */
  --ink:            #1A1A2E;   /* Primary text */
  --slate:          #6B6B8A;   /* Secondary text */
  --slate-light:    #9090B0;   /* Muted text, placeholders */

  /* Surfaces */
  --surface-0:      #F7F7FB;   /* Page background */
  --surface-1:      #F0F0F6;   /* Card inner sections */
  --surface-2:      #FFFFFF;   /* Cards, inputs */

  /* Borders */
  --border:         #E4E4EF;
  --border-strong:  #D0D0E0;

  /* Semantic */
  --bg-success:     #E8F7EF;
  --text-success:   #1A6B3C;
  --border-success: #B8E6CC;
  --bg-warning:     #FFF4E0;
  --text-warning:   #8B5A00;
  --border-warning: #FFD89B;
  --bg-danger:      #FDECEC;
  --text-danger:    #C0392B;
  --border-danger:  #F5C6C6;
  --bg-accent:      #EAF2FF;
  --text-accent:    #1A5F99;
}
```

---

## Typography

Font: **Outfit** — Google Fonts, free
```html
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
```

```css
body {
  font-family: 'Outfit', system-ui, -apple-system, sans-serif;
  -webkit-font-smoothing: antialiased;
}
```

| Use | Size | Weight |
|---|---|---|
| Page heading | 26–36px | 700 |
| Section heading | 18–24px | 700 |
| Card title | 13–14px | 600 |
| Body | 13–14px | 400 |
| Table | 12px | 400/500 |
| Labels | 11px | 500–600 |
| Tags / badges | 10px | 600 |

---

## Sidebar

Background: `#5457E0` (slightly brighter than --purple for visual pop)
Active nav item: `--amber` background, `--ink` text
Hover: `rgba(255,255,255,0.14)` background
Text: `rgba(255,255,255,0.82)` default, `#fff` hover

---

## Buttons

All buttons: white base with subtle shadow. Amber fill on hover. No transparent buttons.

```css
.btn {
  background: #FFFFFF;
  color: #1A1A2E;
  border: 1px solid #D8D8E6;
  box-shadow: 0 1px 2px rgba(26,26,46,.07);
  transition: background .15s, color .15s, border-color .15s;
}
.btn:hover {
  background: #F5A623;
  color: #1A1A2E;
  border-color: #F5A623;
  box-shadow: 0 2px 5px rgba(245,166,35,.35);
}
.btn-primary {
  background: #FFFFFF;
  color: #4F46E5;
  border-color: #6366F1;
}
.btn-primary:hover {
  background: #F5A623;
  color: #1A1A2E;
  border-color: #F5A623;
}
```

---

## Logo

Icon: K formed from a grid of rounded squares. Amber top-left square as accent.
Wordmark: "Klass" in --ink or white, "er" in --amber.
Font in wordmark: Outfit Bold.

Logo files to create:
- logo-light.svg (on white/light backgrounds)
- logo-dark.svg (on dark/purple backgrounds)
- icon-only.svg (for favicon, app icon)

---

## Marketing site

Hero background: `--purple` (#6366F1)
Sections alternate: `#fff` and `#FAFAFE`
Footer: `--ink` (#1A1A2E)
Eyebrow labels: `--purple`, uppercase, letter-spacing 1.2px
CTA button in hero: `--amber` fill

---

## Timetable grid colours

| Subject type | Background | Text |
|---|---|---|
| Default / computing | `--purple-light` | `--purple-dark` |
| Science | `--bg-warning` | `--text-warning` |
| Maths | `--bg-success` | `--text-success` |
| English / humanities | `--bg-accent` | `--text-accent` |
| Mentor / duty | `--surface-1` | `--slate` |

---

## Icon library

Tabler Icons — CDN:
```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.6.0/dist/tabler-icons.min.css">
```

Usage: `<i class="ti ti-home"></i>`

---

## Border radius

```css
--radius-sm:  4px;   /* Tags, badges, small elements */
--radius:     6px;   /* Inputs, buttons, small cards */
--radius-md:  8px;   /* Inner card sections, chips */
--radius-lg:  12px;  /* Cards */
--radius-xl:  16px;  /* Login cards, modals */
```
