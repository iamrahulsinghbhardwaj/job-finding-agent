# Privacy policy: job digest

*Last updated: 28 November 2026*

**Job digest** is a personal, non-commercial tool. It runs this repository's code on a schedule and
emails its owner a digest of job postings. It is not offered to anyone else, and it has one user:
its owner.

## What the app accesses
The app signs in to **one Gmail account**: a dedicated account the owner created only for sending
the digest. It asks Google for two permissions on that account and uses them only as follows:

- **Send email** (`gmail.send`): to send the digest, from that account to the owner's own address.
- **Read email** (`gmail.readonly`): to search that account's **Sent** folder for a digest the app
  sent earlier, so that an interrupted run never sends the same digest twice. It reads only the
  message ID and subject of its own digests. It never reads anything else in the mailbox.

## What it stores
Nothing from the mailbox. After a send, it keeps Gmail's message ID for that digest in its own
database, so it can recognise the digest later. It never keeps email content, contacts or anything
else from Gmail.

## What it shares
Nothing. No Gmail data is sold, shared, sent to third parties, used for advertising, or used to train
AI models. Job postings come from public job boards, not from Gmail.

## Security
The access token is kept as an encrypted secret: in GitHub Actions secrets, and in a local `.env`
file that is never committed. Logs record counts only, never email content or addresses.

## Removing access
The owner can revoke access at any time at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions) (signed in as the bot
account). After that the app can no longer send or read anything.

## Contact
Open an issue on this repository.

The app's use of information received from Google APIs follows the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.
