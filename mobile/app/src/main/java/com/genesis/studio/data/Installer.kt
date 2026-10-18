package com.genesis.studio.data

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import java.io.File

/** Hands a downloaded APK to Android's package installer. The user always confirms the install. */
object Installer {
    private const val APK_MIME = "application/vnd.android.package-archive"

    fun apkDir(context: Context): File = File(context.filesDir, "apks")

    fun canInstall(context: Context): Boolean = context.packageManager.canRequestPackageInstalls()

    fun openInstallPermissionSettings(context: Context) {
        val intent = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
    }

    private fun uriFor(context: Context, apk: File): Uri =
        FileProvider.getUriForFile(context, "${context.packageName}.files", apk)

    fun install(context: Context, apk: File) {
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uriFor(context, apk), APK_MIME)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
    }

    fun share(context: Context, apk: File, appName: String) {
        val send = Intent(Intent.ACTION_SEND)
            .setType(APK_MIME)
            .putExtra(Intent.EXTRA_STREAM, uriFor(context, apk))
            .putExtra(Intent.EXTRA_SUBJECT, "$appName (made with Genesis)")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        context.startActivity(Intent.createChooser(send, "Share $appName").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }
}
