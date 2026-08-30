name: Automated Manhwa Chapter Ingestion

on:
  schedule:
    - cron: '0 */12 * * *'
  workflow_dispatch:

jobs:
  download_and_upload:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code Repository
        uses: actions/checkout@v4

      - name: Set up Python 3.10
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install Dependencies
        run: |
          pip install -r requirements.txt

      - name: Install Playwright Chromium with Dependencies
        run: |
          playwright install --with-deps chromium

      - name: Execute Downloader Pipeline
        env:
          NEXUS_DRIVE_CREDS: ${{ secrets.NEXUS_DRIVE_CREDS }}
          DRIVE_OAUTH_TOKEN: ${{ secrets.DRIVE_OAUTH_TOKEN }}
        run: |
          python main_pipeline.py
