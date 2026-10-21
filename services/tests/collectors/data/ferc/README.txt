Real FERC eLibrary responses captured 2026-10-21 with the declared MardenBot User-Agent.
search_*.json: POST https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch, docket filter,
  posted_date 2026-09-21..2026-10-21, 100 per page (search_failed.json: the answer to a malformed body).
trimmed_*.json: the same responses cut to the hits whose main files are saved here (totalHits/numHits set
  to the kept count); used by the end-to-end test.
file_<accession>.*: POST .../File/DownloadP8File for that filing's main file (sent as application/octet-stream).
robots_www_ferc_gov.txt: https://www.ferc.gov/robots.txt (https://elibrary.ferc.gov/robots.txt answers 404).
challenge_www_ferc_gov.html: what https://www.ferc.gov/privacy-policy answers the bot User-Agent (HTTP 403).
ecfr_18_388_106.xml: eCFR 18 CFR 388.106 (versioner API, 2026-10-01).
pia_elibrary_excerpt.txt: pdftotext of https://www.ferc.gov/sites/default/files/2020-05/PIA-eLibrary.pdf, the
  two passages quoted in the source's terms note.
