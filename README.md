# Seal

Seal gets a client agreement signed before work starts on a file. A file is one job for one client. You upload whatever PDF the agreement happens to be, check the signature fields, and send a link. When every signer has signed, the file is ready to start.

The agreement does not have to be one Seal has seen. Seal looks for short labels (`Signature`, `Printed name`, `Date`, `Initials`) and for existing PDF form fields. When `OPENAI_API_KEY` is set, it also sends the agreement's text to OpenAI, which names the parties and points at the lines to sign, including blank lines with unusual captions. The model picks lines by number and never supplies coordinates. If the call fails, the rules alone place the fields. You can move any box or place new ones. The words of the agreement are not edited. Ink goes into the fields, and a completion page is added at the end.

## Run it

```powershell
.\run.ps1
```

Then open http://127.0.0.1:8000

Load the sample file if you want a fictional agreement to click through. For a real trial, use **New file**, upload a PDF, and open the signing link on that file. Use test names only.

## What a reviewer does

1. Create a file and upload the agreement.
2. Check the boxes on the pages. Drag any that missed the line.
3. Open the signing link (email is optional).
4. Type a name, tick the box, and sign.
5. Download the signed PDF. The file should say ready to start.

## Tests

```powershell
.\.venv\Scripts\python -m pytest
```

## Deploy

One company per deployment. Set `SEAL_DATA` to a **persistent disk**. A host with an ephemeral disk will forget files when it restarts. Set `SEAL_BASE_URL` to the public https address so emailed links are right.

```
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Optional environment variables are listed in `.env.example`. Leave `SEAL_ACCESS_KEY` unset for a review deploy so the site opens on the first visit. Set it before real use. Signing links keep working either way.

## Layout

- `app/main.py` — pages and the signing flow
- `app/pdfutil.py` — find fields, stamp a signature, add the completion page
- `app/ai.py` — optional OpenAI pass that reads the agreement text
- `app/db.py` — SQLite
- `SUBMISSION.md` — draft text for the three written answers

The fonts under `app/static/fonts/` are Great Vibes and Lato, both under the SIL Open Font License. The licenses are in that folder.
