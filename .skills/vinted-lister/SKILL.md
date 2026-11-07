---
name: vinted-lister
description: >-
  Turn a folder of product photos into Vinted draft listings. Use when the user
  drops images into inbox/ and asks to "list", "post", "create Vinted drafts",
  "process my photos", "sell these on Vinted", or similar. Groups photos per
  product, identifies brand/type/size/colour/condition from the images, resolves
  Vinted IDs, applies pricing rules, and creates DRAFT listings via the Python
  client — then files the photos into posted/ and logs each row in LISTINGS.md.
---

# Vinted Lister

Create Vinted **draft** listings from product photos, end to end, programmatically.

## What this does

1. The user drops raw product photos into `inbox/` (a few shots per item).
2. You (the agent) look at the photos, **group them per product**, read the
   brand/size/colour/condition, and pick the right Vinted category.
3. You apply the pricing rules and create a **draft** via `tools/list_product.py`.
4. The engine uploads the photos, creates the draft, moves that product's photos
   to `posted/NN-slug/`, and appends a row to `LISTINGS.md`.
5. Anything you are unsure about goes to `review/` with an `OBS.txt` note.

Posting is 100% programmatic (the Python client in `vinted/`). The only step that
needs *you* is the vision part — identifying what each photo shows.

## Prerequisites (one-time, per machine)

```bash
python3 -m venv .venv
.venv/bin/pip install -e .       # installs deps + the `vinted` command
cp .env.example .env            # then fill in auth (see "Authentication")
.venv/bin/vinted auth import    # paste a DevTools "Copy as cURL" to fill tokens
```

Then drive everything through the `vinted` CLI (activate the venv, or call
`.venv/bin/vinted`):

```bash
vinted whoami
```

## Authentication

`vinted/` talks to Vinted's private web API. Fill `.env` (gitignored) with the
user's own logged-in session — cookies + the headers the web app injects:

- Cookies: `access_token_web`, `refresh_token_web`, `_vinted_fr_session`,
  `datadome`, `anon_id`
- Headers: `x-csrf-token`, `x-anon-id`

The blocker is **not** DataDome — it is the missing `x-csrf-token` header.
`curl_cffi` (Chrome TLS impersonation) handles the rest. Tokens are short-lived;
if calls return `403`/`401`, refresh them: run `vinted auth import` and paste a
fresh DevTools "Copy as cURL" of any `/api/v2/...` request — it parses the cookies
and headers straight into `.env`.

## Step-by-step

### 1. Convert HEIC → JPG
Vinted accepts JPG/PNG/WEBP, **not HEIC**. Convert iPhone photos first
(macOS): `sips -s format jpeg -s formatOptions 80 -Z 2048 in.HEIC --out out.jpg`.

### 2. Group photos per product — CRITICAL
A product is usually **front → label/collar close-up → back**. **Do NOT assume
strict 3-in-a-row by filename.** Decide *visually* whether photos `1..X` belong
to the same item. Numbering can be misleading: a shot retaken at the end (e.g. a
forgotten back photo) gets a **much higher number** but still belongs to an
earlier product. If a product seems to be missing its back/label, look for a
stray later-numbered photo that matches it before treating it as complete. Put
genuine orphans in `review/`.

### 3. Identify each product
- **Brand**: read the neck label (Tommy Hilfiger, Ralph Lauren, …).
- **Type → category**: checked/plaid/gingham → *Rutiga skjortor*; solid →
  *Enfärgade*; striped → *Randiga*; patterned → *Mönstrade*; denim → *Jeansskjortor*.
- **Size**: from the label (letter size, or neck/sleeve like `16 34/35` ≈ L).
- **Colour**, **condition** (default *Mycket bra* / very good unless clearly new),
  **material** (usually cotton).

### 4. Resolve Vinted IDs
- Category: `data/catalog_lookup_se.json` (`id -> path`), or
  `GET /api/v2/item_upload/catalogs/search?keyword=` then resolve against the tree
  (regenerate the tree with `tools/fetch_catalogs.py`). **Never** trust the search
  result order — always resolve the id to its full path.
- Brand: `GET /api/v2/brands?keyword=<brand>`.
- Colour: `GET /api/v2/colors`. Size: `GET /api/v2/size_groups` (men's letters =
  group 14: XS=206 S=207 M=208 L=209 XL=210 XXL=211).
- Condition (dynamic attribute): 6 New with tags, 1 New without tags, 2 Very good,
  3 Good, 4 Satisfactory. Material e.g. Cotton=44. Package size 1 = Small.

### 5. Price
Brand rules live in `config/markets.json` (`brand_prices` of the active market). Brands without a rule →
Vinted's **premium** suggestion (max of the band). Edit the map per the owner.

### 6. Post
Use the CLI. Flags are human-friendly (names auto-resolve to IDs):

```bash
vinted draft \
  --photo inbox/IMG_0001.jpg --photo inbox/IMG_0002.jpg --photo inbox/IMG_0003.jpg \
  --title "Tommy Hilfiger checked shirt L" --description "..." \
  --category "rutiga skjortor" --brand "Tommy Hilfiger" \
  --size L --condition very_good --color Multi --material cotton \
  --n 1 --slug tommy-hilfiger-checked-shirt-l --product "Checked shirt"
```

For automation, pass a fully pre-resolved spec as JSON:

```bash
vinted draft --json '{"n":1,"slug":"tommy-hilfiger-checked-shirt-l",
  "files":["IMG_0001.jpg","IMG_0002.jpg","IMG_0003.jpg"],"title":"...","description":"...",
  "catalog_id":1801,"brand_id":94,"brand":"Tommy Hilfiger","color_ids":[15],
  "size_id":209,"condition_id":2,"material_ids":[44],"price":200,
  "labels":{"product":"Checked shirt","size":"L","condition":"Very good","colour":"Multi"}}'
```

Omit `--price`/`price` to auto-resolve (brand rule, else Vinted premium). The CLI
uploads the photos, creates the draft, moves them to `posted/NN-slug/`, and logs a
row in `LISTINGS.md`. Add `--no-move` / `--no-log` for a one-off manual add.

### CLI reference
```
vinted whoami                      check auth / who you are
vinted auth import [--curl|--file] paste a DevTools cURL -> writes tokens to .env
vinted catalogs <keyword>          search categories (id + full path)
vinted brands <keyword>            search brands (id + title)
vinted colors                      list colour ids
vinted sizes [--group N | --all]   list size ids (default: market's default_size_group)
vinted conditions                  list condition ids
vinted price <image> [--brand]     image-based price band + resolved price
vinted draft ...                   create a draft (flags or --json)
vinted ads [--brand B] [--price P]  list YOUR live ads from the account
vinted item <id|url>               show one ad's full editable data
vinted edit <id|url> [--field ...] edit a LIVE ad (title/description/price/
                                   category/brand/size/condition/color/material)
vinted reprice --brand B --from X --to Y [--dry-run]  bulk reprice live ads
```

## Notes
- **Default to drafts.** Only publish live when the owner explicitly says so.
- Write titles/descriptions in **whatever language the owner asks for**.
- **Image budget**: you can only view a limited number of photos per turn — work
  in batches (downscale to ~800px previews to fit more products per turn).
- **Updating a draft** (`update_draft`/PUT): you must RE-UPLOAD photos and pass
  fresh photo ids — reusing stale upload ids **wipes** the draft's photos.
- **Editing a LIVE (published) ad** uses a different endpoint than drafts:
  `PUT /item_upload/items/{id}` (not `/item_upload/drafts/{id}`, which returns
  `403 access_denied` on published items). `vinted edit` handles this and
  **preserves photos** by reusing the ad's current photo ids — no re-upload.
- Never commit personal data: `inbox/`, `posted/`, `review/` contents, `.env`,
  `LISTINGS.md` and `*.HEIC` are gitignored.
