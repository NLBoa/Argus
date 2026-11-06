# Argus privacy policy

Argus is a personal course hub. One student runs it on their own Raspberry Pi, for their own use. It is not a
service offered to anyone else, and it has no other users.

## Google user data

Argus uses Google Drive for one feature, the file adder: the student picks a file on their own dashboard, and Argus
uploads it to a folder in the student's own Google Drive, where the student's own planning assistant reads it.

- **What Argus accesses.** Only what the upload needs: the destination folder (to check that it exists and accepts
  new files), the files Argus uploads, and the signed-in account's email address, shown once during setup so the
  student can confirm the account. Argus does not read, list, change, or delete any other file, even when the granted
  scope would allow it.
- **Where it is stored.** The OAuth refresh token is stored as a root-only credential file on the student's own
  device. It is never written to the source repository, logs, or any other service. Logs record an upload's file name
  and size, never its contents.
- **Sharing.** Argus does not share, sell, or transfer Google user data to anyone. It sends no data to any server
  except Google's own APIs.
- **AI.** Argus does not use Google user data to train or improve any AI model.
- **Revoking access.** Remove Argus at [myaccount.google.com/connections](https://myaccount.google.com/connections)
  and delete the credential files from the device.

Argus's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Contact

Open an issue on this repository.
