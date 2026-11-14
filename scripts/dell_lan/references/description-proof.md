# Draft MR description proof

The Dell writer reads the description file as raw bytes, validates strict
UTF-8, and binds its byte count and SHA-256 into the MAC-bound request. Lenovo
re-reads the file without newline translation and requires both facts to match
before import, push, or MR work. A missing, replaced, undecodable, or legacy
request without those integrity fields is refused at the request stage; a path
alone is not proof.

After an MR write, the driver re-reads the single MR and requires exact equality
with the full signed text. Create and update both converge with
`replace_description=True`; appending would make the expected update depend on
unsigned remote state. A changed prefix is therefore a refused
`description-mismatch`, not a successful suffix match. Missing or unreadable
remote text fails closed as unknown (`mr-readback-unavailable`).

Reconciliation performs the same single-MR comparison before reporting
`observed`. Dell readback requires matching signed-expected and observed
SHA-256 facts from the publication driver before it can set
`publication.bound` for live evidence. A direct `mr_identity.py capture`
receipt proves identity and commit SHAs but carries no signed-description proof,
so it cannot become a live bound publication. The driver `mr` receipt records
both hashes and `descriptionVerified: true`.
