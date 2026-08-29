# School onboarding importer

Turns the two filled workbooks in `tools/templates/` into a live school.

```bash
pip install -r tools/requirements.txt

# look, change nothing (the default)
python tools/import_school.py --setup Greenfield_Setup.xlsx

# …including students and parents
python tools/import_school.py --setup Greenfield_Setup.xlsx --people Greenfield_People.xlsx

# actually write it
python tools/import_school.py --setup Greenfield_Setup.xlsx --apply
```

Needs `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` — only to `--apply`; a dry
run needs neither, and never opens a connection.

## What it will and will not do

**Dry run is the default.** Nothing is written without `--apply`, and nothing is
written at all while the workbook has a single error. A half-imported school is
worse than an unimported one, because the school cannot tell which half.

**It never sends an email.** Staff and parents get an invite ROW; the links are
written to a CSV for the school to hand out on its own terms. Students get
accounts and temporary passwords, also to a CSV — shown once, resettable but not
recoverable. Delete your copy once the school has it.

**It is idempotent.** Re-running matches on the keys the school gave us — school
by web address, staff by email, class by name, student by username, link by pair
— and updates instead of duplicating. Onboarding always ends with "one more
teacher joined": that is a re-run, not a repair job.

## Run it twice, deliberately

Classes need a class teacher (`classes.teacher_id` is NOT NULL) and parent links
need a parent account, so on the first pass those wait for people to accept their
invites. The importer says exactly which ones it skipped and why. Once staff are
in, run the same file again and the rest lands.

## Layout

| file | does |
|---|---|
| `school_import/parse.py` | reads and validates the workbooks. Pure — no network, no supabase import, fully tested in `tests/test_school_import.py` |
| `school_import/apply.py` | the only thing that writes |
| `import_school.py` | the CLI |
| `templates/` | the canonical workbooks. Their column headings are the contract, which is why they are versioned next to the parser and the tests read them |

The column headings and the tab names ARE the interface. A school that renames a
tab gets a loud failure, not a silent skip.
