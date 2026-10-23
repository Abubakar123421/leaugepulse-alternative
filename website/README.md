# Legacy — Marketing & Docs Website

A static, dependency-free glassmorphism website for the **Legacy** Madden league Discord bot:
landing page with a cinematic scroll-driven hero, a searchable 50-command reference, a full
operator documentation page, and an FAQ / recovery playbook.

## Pages

| File            | Purpose                                                            |
| --------------- | ------------------------------------------------------------------ |
| `index.html`    | Landing page — hero, stats, features, weekly timeline, showcases   |
| `commands.html` | All 50 slash commands with search, access filters, and copy buttons |
| `docs.html`     | Operator guide — invite, launch, imports, matchups, seasons, hosting |
| `faq.html`      | FAQ / recovery playbook with accordion answers                     |

No build step and no framework — plain HTML/CSS/JS. Host it on anything that serves
static files (Netlify, Vercel, GitHub Pages, Cloudflare Pages, shared hosting…).

## Run locally

```bash
npx serve website -l 4173
```

`website/serve.json` enables clean URLs (`/docs` ⇄ `/docs.html`) for the `serve` package.
Netlify/Vercel have their own equivalent ("Pretty URLs" / `cleanUrls`) — optional either way,
since every internal link uses the full `.html` form.

## Discord community link

Every **Join Our Discord** button points to the official Legacy community server:

`
https://discord.gg/3kXKNqNHM7
`

Update this URL in the four HTML files only if the server invite changes.
## The hero video (AI, via ImagineArt)

The landing hero is a **scroll-choreographed cinematic**. Out of the box it runs on a
canvas "stadium night" scene (beams, particles, perspective field). If a video file
exists at:

```
website/assets/video/hero.mp4     (or hero.webm)
```

...the hero automatically switches to the video: it plays as a slow ambient loop
(muted, 0.85x speed) while scrolling drives a parallax zoom and the headline-to-beat
crossfade. Scroll intentionally does not seek the decoder; per-frame currentTime
scrubbing stutters on normally-encoded footage.
Tips:
- 5–15 s loops work best; slow, dark footage keeps the headline readable.
- Optional slimming (drops the unused audio track, faster first paint):
  `ffmpeg -i in.mp4 -an -crf 23 -movflags +faststart hero.mp4`

## Brand assets

Source art lives in `output/branding/` (repo root). The site uses processed copies in
`assets/img/`: `legacy-logo.png` (transparent, flood-filled background), `logo-128.png`,
`favicon.png`, `legacy-banner.jpg` (CTA background + Open Graph image).

## Updating the command reference

All command content is data, not markup: edit the `LEGACY_COMMANDS` entries in
`assets/js/data.js` (name, category, access, syntax, purpose, example, notes) and the
page re-renders itself. Categories live in `LEGACY_CATEGORIES` in the same file.
