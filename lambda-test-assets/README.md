# Shared synthetic fixtures for Lambda local testing

These tiny, synthetic files are uploaded by the wizard server's "Seed
Resources" step into the Lambda's expected S3 bucket(s) so handlers
that read S3 objects have something to read in Floci.

| File              | Type                | Use case                                          |
| ----------------- | ------------------- | ------------------------------------------------- |
| `sample-tiny.pdf` | Real 1-page PDF     | PDF compressors / merger / page-counter Lambdas   |
| `sample-tiny.png` | 1x1 RGBA PNG        | Image optimiser / resizer Lambdas                 |
| `sample-tiny.jpg` | 1x1 JPEG            | Image optimiser fallback                          |
| `sample-tiny.svg` | Tiny safe SVG       | SVG sanitiser Lambdas                             |
| `sample.csv`      | 4-row CSV           | Bulk import / data-stream Lambdas                 |
| `sample.json`     | Small JSON object   | Webhook payload / event fixtures                  |
| `sample.txt`      | ASCII text          | Generic                                           |

How `config/lambda-test-overrides.json` references these:

```json
{
  "beaconstac_pdf_compressor": {
    "fixture_keys": {
      "test-document.pdf": "fixture://sample-tiny.pdf"
    }
  }
}
```

`fixture://<filename>` resolves to `lambda-test-assets/<filename>` at
seed time. If the override key doesn't exist in the asset library, the
seed step fails fast.

These files are intentionally tiny so the repo doesn't bloat. If you
need a more realistic fixture (e.g. a multi-page PDF for testing
compression ratios), drop it next to these files and reference it via
`fixture://your-file.pdf`.
