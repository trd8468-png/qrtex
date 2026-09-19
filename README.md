# QRTex — Permanent QR Note

Flow: QR -> permanent URL -> hosted frontend -> notes.js -> displayed text.

The QR contains only the URL, not the text.

## Add a permanent note
Edit `notes.js`:

```js
window.NOTES = {
  "my-note": {
    title: "My Note",
    text: "This is my permanent statement."
  }
};
```

Then deploy and make the QR point to:
`https://YOUR-DOMAIN/n/my-note`

No login, authorization, database, backend API, random daily QR, or expiry mechanism.

The domain and deployment must remain online for scans to work.