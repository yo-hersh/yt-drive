# yt-drive

הורדה מ-YouTube ישירות ל-Google Drive דרך GitHub Actions. הקבצים לא עוברים דרך המחשב.

## הפעלה
- **באתר או באפליקציית GitHub:** Actions ← Download to Drive ← Run workflow ← להדביק קישור.
- **מהמחשב:**
  ```bash
  gh workflow run download.yml -R yo-hersh/yt-drive -f url=https://youtu.be/XXXX
  ```
  אפשר להוסיף `-f mode=mp3`, `-f quality=720`, `-f items=3-7`, `-f whole_playlist=true`, `-f folder=Music`.

הקבצים מגיעים לתיקייה `YouTube` בדרייב, או לתיקייה שנבחרה. פריטים מפלייליסט נשמרים בתת-תיקייה בשם הפלייליסט.

## Secrets (Settings ← Secrets and variables ← Actions)
| שם | תוכן |
|---|---|
| `DRIVE_URL` | כתובת ה-Web app של ה-Apps Script שב-`apps-script/` |
| `DRIVE_KEY` | המפתח שמופיע ב-`KEY` בתוך ה-Apps Script |
| `YT_COOKIES` | תוכן cookies.txt מחשבון מחובר, אם YouTube חוסם ("Sign in to confirm you're not a bot") |

## איך ההעלאה לדרייב עובדת
ה-Apps Script רץ בחשבון Google שלך ומחזיר אישור גישה זמני לשעה, בהרשאת `drive.file`. בהרשאה הזו הוא רואה רק קבצים שהוא עצמו יצר. ה-workflow מבקש אישור כזה עם `DRIVE_KEY` ומעלה את הקבצים ישירות ל-Drive API. לא צריך פרויקט ב-Google Cloud.

## שפה
ההודעות נמצאות ב-`lang/*.json`. לשפה חדשה מוסיפים קובץ ואת הקוד שלו לרשימה `lang` ב-`.github/workflows/download.yml`.
