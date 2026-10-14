# yt-drive

הורדה מ-YouTube ישירות ל-Google Drive דרך GitHub Actions. הקבצים לא עוברים דרך המחשב.

## הפעלה
- **באתר או באפליקציית GitHub:** Actions ← Download to Drive ← Run workflow ← להדביק קישור.
- **מהמחשב:**
  ```bash
  gh workflow run download.yml -R yo-hersh/yt-drive -f url=https://youtu.be/XXXX
  ```
  אפשר להוסיף `-f mode=mp3`, `-f quality=720`, `-f items=3-7`, `-f whole_playlist=true`, `-f folder=Music`, `-f stop_after_minutes=30` (לשידור חי: עוצר אחרי 30 דקות ומעלה את מה שהוקלט).

הקבצים מגיעים לתיקייה `YouTube` בדרייב, או לתיקייה שנבחרה. פריטים מפלייליסט נשמרים בתת-תיקייה בשם הפלייליסט.

## Secrets (Settings ← Secrets and variables ← Actions)
| שם | תוכן |
|---|---|
| `DRIVE_URL` | כתובת ה-Web app של ה-Apps Script שב-`apps-script/` |
| `DRIVE_KEY` | המפתח שמופיע ב-`KEY` בתוך ה-Apps Script |
| `RCLONE_CONF` | רק לדרך `rclone`: קובץ ההגדרות של rclone עם חיבור בשם `gdrive`, שנוצר עם ה-client_id שלך |
| `YT_COOKIES` | תוכן cookies.txt מחשבון מחובר, אם YouTube חוסם ("Sign in to confirm you're not a bot") |

## איך ההעלאה לדרייב עובדת
ה-Apps Script רץ בחשבון Google שלך ומחזיר אישור גישה זמני לשעה, בהרשאת `drive.file`. בהרשאה הזו הוא רואה רק קבצים שהוא עצמו יצר. ה-workflow מבקש אישור כזה עם `DRIVE_KEY` ומעלה את הקבצים ישירות ל-Drive API. לא צריך פרויקט ב-Google Cloud.

## שתי דרכי העלאה (השדה "דרך ההעלאה לדרייב" ב-Run workflow)
| | `apps_script` (ברירת מחדל) | `rclone` |
|---|---|---|
| מה צריך | Apps Script פרוס + `DRIVE_URL`, `DRIVE_KEY` | OAuth client_id משלך מ-Google Cloud + `RCLONE_CONF` |
| תיקייה בדרייב | נפרדת. כל דרך רואה רק קבצים שהיא עצמה יצרה (`drive.file`) | נפרדת |

שתי הדרכים משתמשות בהרשאה `drive.file`, ולכן כל אחת מהן תיצור לעצמה תיקיית `YouTube` משלה. כדאי לבחור דרך קבועה, או לתת לכל דרך שם תיקייה אחר.

### הקמת rclone עם client_id משלך (פעם אחת, במחשב)
```powershell
rclone config delete gdrive
rclone config create gdrive drive client_id=<CLIENT_ID> client_secret=<CLIENT_SECRET> scope=drive.file
Get-Content "$env:APPDATAcloneclone.conf" -Raw | gh secret set RCLONE_CONF -R yo-hersh/yt-drive
```

## שפה
ההודעות נמצאות ב-`lang/*.json`. לשפה חדשה מוסיפים קובץ ואת הקוד שלו לרשימה `lang` ב-`.github/workflows/download.yml`.
