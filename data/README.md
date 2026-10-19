# data/

The real catalogue exports and the distributor's logo are confidential and are
not part of this repository. What is here instead:

| File | What it is |
|---|---|
| `sample_catalogue.csv` | Ten made-up rows in the same shape as the real export (`Item Number`, `Pack`, `Size / UOM`, `Description`, `Retail`, `Price`). Item numbers and prices are invented. |
| `logo.png` | A placeholder watermark. Replace it with your own logo. |

Point the pipeline at the sample with:

```bash
INPUT_CSV=data/sample_catalogue.csv make source LIMIT=3
```

Any `.csv` or `.xlsx` export with those column names works the same way.
