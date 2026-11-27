# Submission draft

Edit this into your own words before you paste it. The form asks for three answers, a public GitHub URL (or add `theastraway` as a collaborator), and a URL where the app is running.

## Description of what you built

Seal is a web app for one company. A file is one job for one client, and that file is not ready to start until the agreement is signed. You create the file, enter the client, and upload the agreement as a PDF. The agreement does not have to be one the app has seen before. Seal sends the agreement's text to OpenAI, which names the parties and points at each line where someone signs, prints a name, or dates, including bare blank lines with unusual captions. Built-in rules also look for short signature, name, date, and initials labels and for existing PDF form fields, and fill in anything the model missed. If the pages are a scan, or both passes miss a line, you click the page to place the box yourself, then drag it onto the line. Fields start assigned to the client. If a block looks like the other party's signature, it is marked so you can add that signer or delete the field. Email does not go out until you confirm you checked the fields.

The signing link works when email is not configured, so the whole path can be finished in the browser. The signer types or draws a signature. That ink is written only into the boxes. The agreement's words are not changed. When every signer has signed, Seal adds a last page with the original file's SHA-256, the time, the IP address, and the activity on the file. The file then shows as ready to start, and the signed PDF can be downloaded.

I built this as one company per deployment, with the database and PDFs stored in a folder on that server, because that is something a firm could run now. A hosted product for every other company in the industry, with separate tenants and billing, is a later step. It does not help this first agreement get signed. I used Python, FastAPI, PyMuPDF, SQLite, and the OpenAI API. I used Cursor and its AI agent (Claude) as a coding assistant.

## Features you included, and why

The unit of work is a file, with a status that moves from preparing to ready to start, because that is the thing the client said was getting stuck.

Any PDF can be uploaded. An OpenAI model reads the text, because an unseen agreement can phrase its signature block in ways no fixed list of labels covers. The model picks lines by number and never supplies coordinates. Seal computes the box from the line's real position, so a bad answer can only choose the wrong line, not draw a box in the wrong place. If the call fails or no key is set, the rules alone place the fields, so signing never depends on the model. The file page says what the model found, or that it did not run. You can place, move, and resize boxes by hand, because detection on an agreement the app has never seen will sometimes be wrong and a person has to be able to fix it. Found fields start on the client. A field that looks like the other party's is marked rather than inventing a second signer with no email.

Email waits for a checkbox that the fields were checked. The link itself is on the page immediately, so signing does not depend on an email server and a review can finish without one. Optional SMTP sends the same link. A reminder can be recorded, and emailed when SMTP is set, because chasing a signature is part of the manual work this replaces.

More than one signer is supported, and each field belongs to one of them. One action applies a typed or drawn signature, initials, the printed name, and the UTC date. The original upload is kept. A working copy receives only the field ink, and a completion page is added so the file has a record: hash, time, IP, and activity. That page says it is not a government certificate.

You can pull a link back before anyone has signed. A workspace key is an environment variable you can turn on later. Signing links stay usable without it, which is what the client needs, and what a reviewer needs on the first visit. A fictional sample file is included so the path can be tried without anyone's real agreement. A test-data warning stays on every page.

## Features you deliberately left out, and why

Qualified certificates, identity proofing, and cryptographic sealing are out. The completion page is a business record of who signed and when. Calling it a legal digital certificate would be a false claim. A firm that needs a qualified signature can still use this to run the workflow and can swap in a certified provider later.

Multi-tenant accounts, billing, and team permissions are out. One deployment is one company. That is a product a firm can run today. A hosted service for the whole industry needs tenants and payment, and neither of those gets the MIND agreement signed. I would add them after this path is in daily use.

Automatic reminder schedules, SMS, and a background worker are out. A person records the chase in one click. A scheduler is extra machinery for a first version, and it fails quietly when the server sleeps.

Reading scanned pages with a vision model is out. Only the text is sent to OpenAI. Scans fall back to placing boxes by hand, which takes seconds for a one-page signature block. Sending page images would cost more per upload and send more of the client's document to a third party.

Editing the agreement, redlines, and a library of templates are out. The agreement arrives unknown, and the point of the product is to get that document signed, not to rewrite it.

Native apps, CRM integrations, single sign-on, and password reset are out. The signing page is a normal browser page, including on a phone. The workspace key, when you set one, lives in the server environment. If it is lost, you change that variable. There is no account system to reset.
