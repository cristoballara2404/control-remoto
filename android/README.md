# App Android

Aquí va el **proyecto completo de Android Studio** (la carpeta que contiene `app/`, `gradle/`,
`build.gradle.kts`, `settings.gradle.kts`, etc.), no solo `MainActivity.kt`.

Antes de subirlo:

- Confirma que NO se suben `local.properties`, las carpetas `build/` y `.gradle/`, ni ningún `.jks`/`.keystore`
  (el `.gitignore` de la raíz ya los excluye).
- Permisos necesarios en `AndroidManifest.xml`: `INTERNET` y `ACCESS_NETWORK_STATE`; en la `<activity>`,
  `android:configChanges="orientation|screenSize|screenLayout|keyboardHidden|smallestScreenSize|uiMode"`.
- Dependencia: `com.squareup.okhttp3:okhttp:4.12.0`.
- `minSdk` 24.

El código principal está en `app/src/main/java/.../MainActivity.kt`.
