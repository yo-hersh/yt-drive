// Gives the GitHub workflow a short-lived Google Drive token (drive.file scope: only files this
// script's uploads create). Deploy as a web app: Execute as "Me", access "Anyone".
// Replies with JSON: {"token": "..."} or {"error": "<stable code>"}.
const KEY = 'PUT_KEY_HERE';

function doPost(e) {
  let body = {};
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply_({ error: 'bad_request' });
  }
  if (!KEY || KEY === 'PUT_KEY_HERE' || body.key !== KEY) {
    return reply_({ error: 'bad_key' });
  }
  return reply_({ token: ScriptApp.getOAuthToken() });
}

// Run once from the editor to grant the Drive permission.
function authorize() {
  ScriptApp.getOAuthToken();
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
