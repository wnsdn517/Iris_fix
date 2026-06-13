package party.qwer.iris

import android.app.RemoteInput
import android.content.ComponentName
import android.content.ContentUris
import android.content.ContentValues
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.MediaStore
import android.util.Base64
import androidx.core.content.FileProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import party.qwer.iris.Replier.Companion.SendMessageRequest
import java.io.File

// SendMsg : ye-seola/go-kdb

class Replier {
    companion object {
        private val messageChannel = Channel<SendMessageRequest>(Channel.CONFLATED)
        private val coroutineScope = CoroutineScope(Dispatchers.IO)
        private var messageSenderJob: Job? = null
        private val mutex = Mutex()

        init {
            startMessageSender()
        }

        fun startMessageSender() {
            coroutineScope.launch {
                if (messageSenderJob?.isActive == true) {
                    messageSenderJob?.cancelAndJoin()
                }
                messageSenderJob = launch {
                    for (request in messageChannel) {
                        try {
                            mutex.withLock {
                                request.send()
                                delay(Configurable.messageSendRate)
                            }
                        } catch (e: Exception) {
                            System.err.println("Error sending message from channel: $e")
                        }
                    }
                }
            }
        }

        fun restartMessageSender() {
            startMessageSender()
        }

        private fun sendMessageInternal(
            referer: String,
            chatId: Long,
            msg: String,
            threadId: Long?
        ) {
            val intent = Intent().apply {
                component = ComponentName(
                    "com.kakao.talk", "com.kakao.talk.notification.NotificationActionService"
                )
                putExtra("noti_referer", referer)
                putExtra("chat_id", chatId)

                putExtra("is_chat_thread_notification", threadId != null)
                if (threadId != null) {
                    putExtra("thread_id", threadId)
                }

                action = "com.kakao.talk.notification.REPLY_MESSAGE"

                val results = Bundle().apply {
                    putCharSequence("reply_message", msg)
                }

                val remoteInput = RemoteInput.Builder("reply_message").build()
                RemoteInput.addResultsToIntent(arrayOf(remoteInput), this, results)
            }

            AndroidHiddenApi.startService(intent)
        }

        fun sendMessage(referer: String, chatId: Long, msg: String, threadId: Long?) {
            coroutineScope.launch {
                messageChannel.send(SendMessageRequest {
                    sendMessageInternal(
                        referer, chatId, msg, threadId
                    )
                })
            }
        }


        fun sendFile(room: Long, filePath: String) {
            coroutineScope.launch {
                messageChannel.send(SendMessageRequest {
                    sendFileInternal(room, filePath)
                })
            }
        }

        private fun sendFileInternal(room: Long, filePath: String) {
            val file = File(filePath)
            mediaScan(Uri.fromFile(file))
            val uri = getContentUri(file)
            val mimeType = getMimeType(file.extension.lowercase())

            val flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                Intent.FLAG_ACTIVITY_CLEAR_TOP or
                Intent.FLAG_GRANT_READ_URI_PERMISSION

            // audio/* and text/* → ACTION_SEND_MULTIPLE (audio sends as voice message, text works correctly)
            val intent = if (mimeType.startsWith("audio/") || mimeType.startsWith("text/")) {
                Intent(Intent.ACTION_SEND_MULTIPLE).apply {
                    setPackage("com.kakao.talk")
                    type = if (mimeType.startsWith("text/")) "*/*" else mimeType
                    putParcelableArrayListExtra(Intent.EXTRA_STREAM, arrayListOf(uri))
                    putExtra("key_id", room)
                    putExtra("key_type", 1)
                    putExtra("key_from_direct_share", true)
                    addFlags(flags)
                }
            } else {
                Intent(Intent.ACTION_SEND).apply {
                    setPackage("com.kakao.talk")
                    type = mimeType
                    putExtra(Intent.EXTRA_STREAM, uri)
                    putExtra("key_id", room)
                    putExtra("key_type", 1)
                    putExtra("key_from_direct_share", true)
                    addFlags(flags)
                }
            }
            try {
                AndroidHiddenApi.startActivity(intent)
            } catch (e: Exception) {
                System.err.println("Error sending file: $e")
                throw e
            }
        }

        fun sendFileWithAttachment(room: Long, filePath: String, attachment: String) {
            coroutineScope.launch {
                messageChannel.send(SendMessageRequest {
                    sendFileWithAttachmentInternal(room, filePath, attachment)
                })
            }
        }

        private fun sendFileWithAttachmentInternal(room: Long, filePath: String, attachment: String) {
            val file = File(filePath)
            mediaScan(Uri.fromFile(file))
            val uri = getContentUri(file)

            val intent = Intent(Intent.ACTION_SEND).apply {
                setPackage("com.kakao.talk")
                type = getMimeType(file.extension.lowercase())
                putExtra(Intent.EXTRA_STREAM, uri)
                putExtra("EXTRA_CHAT_ATTACHMENT", attachment)
                putExtra("key_id", room)
                putExtra("key_type", 1)
                putExtra("key_from_direct_share", true)
                component = ComponentName(
                    "com.kakao.talk",
                    "com.kakao.talk.activity.RecentExcludeIntentFilterActivity"
                )
                addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                    Intent.FLAG_ACTIVITY_CLEAR_TOP or
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                )
            }

            try {
                AndroidHiddenApi.startActivity(intent)
            } catch (e: Exception) {
                System.err.println("Error sending file with attachment: $e")
                throw e
            }
        }

        fun sendWithAttachment(room: Long, msg: String, attachment: String) {
            coroutineScope.launch {
                messageChannel.send(SendMessageRequest {
                    sendWithAttachmentInternal(room, msg, attachment)
                })
            }
        }

        private fun sendWithAttachmentInternal(room: Long, msg: String, attachment: String) {
            val intent = Intent(Intent.ACTION_SEND).apply {
                setPackage("com.kakao.talk")
                type = "text/plain"
                putExtra(Intent.EXTRA_TEXT, msg)
                putExtra("EXTRA_CHAT_ATTACHMENT", attachment)
                putExtra("key_id", room)
                putExtra("key_type", 1)
                putExtra("key_from_direct_share", true)
                component = ComponentName(
                    "com.kakao.talk",
                    "com.kakao.talk.activity.RecentExcludeIntentFilterActivity"
                )
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            }

            try {
                AndroidHiddenApi.startActivity(intent)
            } catch (e: Exception) {
                System.err.println("Error sending message with attachment: $e")
                throw e
            }
        }

        fun sendPhoto(room: Long, base64ImageDataString: String) {
            coroutineScope.launch {
                messageChannel.send(SendMessageRequest {
                    sendPhotoInternal(
                        room, base64ImageDataString
                    )
                })
            }
        }

        fun sendMultiplePhotos(room: Long, base64ImageDataStrings: List<String>) {
            coroutineScope.launch {
                messageChannel.send(SendMessageRequest {
                    sendMultiplePhotosInternal(
                        room, base64ImageDataStrings
                    )
                })
            }
        }

        private fun sendPhotoInternal(room: Long, base64ImageDataString: String) {
            sendMultiplePhotosInternal(room, listOf(base64ImageDataString))
        }

        private fun sendMultiplePhotosInternal(room: Long, base64ImageDataStrings: List<String>) {
            val picDir = File(IMAGE_DIR_PATH).apply { if (!exists()) mkdirs() }

            // Images are written into KakaoTalk's own external-data directory so KakaoTalk
            // can read them directly — no MediaStore URI or content:// dance needed.
            val uris = base64ImageDataStrings.mapIndexed { idx, base64 ->
                val imageFile = File(picDir, "${System.currentTimeMillis()}_${idx}.png").apply {
                    writeBytes(Base64.decode(base64, Base64.DEFAULT))
                }
                Uri.fromFile(imageFile)
            }

            if (uris.isEmpty()) {
                System.err.println("No image URIs created, cannot send multiple photos.")
                return
            }

            val intent = Intent(Intent.ACTION_SEND_MULTIPLE).apply {
                setPackage("com.kakao.talk")
                type = "image/*"
                putParcelableArrayListExtra(Intent.EXTRA_STREAM, ArrayList(uris))
                putExtra("key_id", room)
                putExtra("key_type", 1)
                putExtra("key_from_direct_share", true)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            }

            try {
                AndroidHiddenApi.startActivity(intent)
            } catch (e: Exception) {
                System.err.println("Error starting activity for sending multiple photos: $e")
                throw e
            }
        }


        internal fun interface SendMessageRequest {
            suspend fun send()
        }

        private fun getMimeType(ext: String): String = when (ext) {
            "mp4", "mkv", "avi", "mov", "wmv", "flv", "ts", "mpg", "mpeg" -> "video/*"
            "mp3" -> "audio/mpeg"
            "aac" -> "audio/aac"
            "ogg" -> "audio/ogg"
            "m4a" -> "audio/mp4"
            "wav" -> "audio/wav"
            "flac" -> "audio/flac"
            "wma" -> "audio/x-ms-wma"
            "tta" -> "audio/x-tta"
            "tak" -> "audio/x-tak"
            "pdf" -> "application/pdf"
            "zip" -> "application/zip"
            "gz" -> "application/gzip"
            "rar" -> "application/x-rar-compressed"
            "7z" -> "application/x-7z-compressed"
            "txt" -> "text/plain"
            "md" -> "text/markdown"
            "csv" -> "text/csv"
            "docx" -> "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            "xlsx" -> "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            "pptx" -> "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            "hwp" -> "application/x-hwp"
            else -> "*/*"
        }

        // Obtained the same way as the `content` CLI tool; cached for reuse.
        private val systemContext: android.content.Context? by lazy {
            try {
                val cls = Class.forName("android.app.ActivityThread")
                val thread = cls.getMethod("systemMain").invoke(null)
                cls.getMethod("getSystemContext").invoke(thread) as? android.content.Context
            } catch (e: Exception) {
                System.err.println("[Replier] systemContext unavailable: $e")
                null
            }
        }

        private val audioExts = setOf("mp3","aac","ogg","m4a","wav","flac","tta","tak","wma")
        private val videoExts = setOf("mp4","mkv","avi","mov","wmv","flv","ts","mpg","mpeg","m4v")
        private val imageExts = setOf("jpg","jpeg","png","gif","bmp","webp")

        private fun mediaTableUri(ext: String): Uri? = when {
            ext in audioExts -> MediaStore.Audio.Media.EXTERNAL_CONTENT_URI
            ext in videoExts -> MediaStore.Video.Media.EXTERNAL_CONTENT_URI
            ext in imageExts -> MediaStore.Images.Media.EXTERNAL_CONTENT_URI
            else -> null
        }

        private fun getContentUri(file: File): Uri {
            val appCtx = try {
                Class.forName("android.app.ActivityThread")
                    .getMethod("currentApplication").invoke(null) as? android.content.Context
            } catch (_: Exception) { null } ?: try {
                Class.forName("android.app.AppGlobals")
                    .getMethod("getInitialApplication").invoke(null) as? android.content.Context
            } catch (_: Exception) { null }

            if (appCtx != null) {
                return try {
                    FileProvider.getUriForFile(appCtx, "${appCtx.packageName}.fileprovider", file)
                } catch (_: Exception) {
                    queryOrFallbackUri(file)
                }
            }
            return queryOrFallbackUri(file)
        }

        private fun queryOrFallbackUri(file: File): Uri {
            // 1. Already indexed in MediaStore?
            queryMediaStoreUri(file)?.let { return it }
            // 2. Synchronous scan (Android 11+/API 30+): returns content:// URI immediately
            scanAndGetUri(file)?.let { return it }
            // 3. Direct insert into MediaStore
            insertMediaStoreEntry(file)?.let { return it }
            // 4. Force async scan, poll for up to ~2s
            mediaScanAm(file)
            for (delay in longArrayOf(300L, 600L, 1000L)) {
                Thread.sleep(delay)
                queryMediaStoreUri(file)?.let { return it }
            }
            return Uri.fromFile(file)
        }

        private fun queryMediaStoreUri(file: File): Uri? {
            val tableUri = mediaTableUri(file.extension.lowercase()) ?: return null
            val paths = listOfNotNull(
                try { file.canonicalPath } catch (_: Exception) { null },
                file.absolutePath
            ).distinct()

            // In-process via ContentResolver (fastest, no shell spawn)
            systemContext?.contentResolver?.let { resolver ->
                for (path in paths) {
                    val cursor = resolver.query(
                        tableUri,
                        arrayOf(MediaStore.MediaColumns._ID),
                        "${MediaStore.MediaColumns.DATA} = ?",
                        arrayOf(path), null
                    ) ?: continue
                    cursor.use {
                        if (it.moveToFirst()) {
                            return ContentUris.withAppendedId(tableUri, it.getLong(0))
                        }
                    }
                }
                return null
            }

            // Fallback: shell `content query`
            val sub = when (tableUri) {
                MediaStore.Audio.Media.EXTERNAL_CONTENT_URI -> "audio/media"
                MediaStore.Video.Media.EXTERNAL_CONTENT_URI -> "video/media"
                else -> "images/media"
            }
            for (path in paths) {
                runMediaStoreQuery("content://media/external/$sub", path)?.let { return it }
            }
            return null
        }

        private fun runMediaStoreQuery(tableUri: String, path: String): Uri? {
            return try {
                val escaped = path.replace("'", "''")
                val proc = ProcessBuilder(
                    "content", "query",
                    "--uri", tableUri,
                    "--where", "_data='$escaped'",
                    "--projection", "_id"
                ).redirectErrorStream(true).start()
                val output = proc.inputStream.bufferedReader().readText()
                if (!proc.waitFor(3, java.util.concurrent.TimeUnit.SECONDS)) { proc.destroyForcibly(); return null }
                Regex("_id=(\\d+)").find(output)?.groupValues?.get(1)?.let { Uri.parse("$tableUri/$it") }
            } catch (_: Exception) { null }
        }

        private fun scanAndGetUri(file: File): Uri? {
            val path = try { file.canonicalPath } catch (_: Exception) { file.absolutePath }

            // In-process via ContentResolver.call (what `content call` runs internally)
            systemContext?.contentResolver?.let { resolver ->
                return try {
                    @Suppress("DEPRECATION")
                    resolver.call(Uri.parse("content://media"), "scan_file", path, null)
                        ?.getParcelable("uri")
                } catch (_: Exception) { null }
            }

            // Fallback: shell `content call --method scan_file`
            return try {
                val proc = ProcessBuilder(
                    "content", "call",
                    "--uri", "content://media",
                    "--method", "scan_file",
                    "--arg", path
                ).redirectErrorStream(true).start()
                val output = proc.inputStream.bufferedReader().readText()
                if (!proc.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) { proc.destroyForcibly(); return null }
                // Result: Bundle[{uri=content://media/external/audio/media/123}]
                Regex("uri=(content://[^}\\s,]+)").find(output)?.groupValues?.get(1)?.let { Uri.parse(it) }
            } catch (_: Exception) { null }
        }

        private fun insertMediaStoreEntry(file: File): Uri? {
            val mimeType = getMimeType(file.extension.lowercase())
            val tableUri = when {
                mimeType.startsWith("audio/") -> MediaStore.Audio.Media.EXTERNAL_CONTENT_URI
                mimeType.startsWith("video/") -> MediaStore.Video.Media.EXTERNAL_CONTENT_URI
                else -> return null
            }
            val path = try { file.canonicalPath } catch (_: Exception) { file.absolutePath }

            // In-process via ContentResolver.insert
            systemContext?.contentResolver?.let { resolver ->
                return try {
                    resolver.insert(tableUri, ContentValues().apply {
                        put(MediaStore.MediaColumns.DATA, path)
                        put(MediaStore.MediaColumns.DISPLAY_NAME, file.name)
                        put(MediaStore.MediaColumns.MIME_TYPE, mimeType)
                    })
                } catch (_: Exception) { null }
            }

            // Fallback: shell `content insert`
            val sub = if (mimeType.startsWith("audio/")) "audio/media" else "video/media"
            for (volume in listOf("external", "external_primary")) {
                runMediaStoreInsert("content://media/$volume/$sub", path, file.name, mimeType)?.let { return it }
            }
            return null
        }

        private fun runMediaStoreInsert(tableUri: String, path: String, name: String, mime: String): Uri? {
            return try {
                val proc = ProcessBuilder(
                    "content", "insert",
                    "--uri", tableUri,
                    "--bind", "_data:s:$path",
                    "--bind", "_display_name:s:$name",
                    "--bind", "mime_type:s:$mime"
                ).redirectErrorStream(true).start()
                val output = proc.inputStream.bufferedReader().readText()
                if (!proc.waitFor(3, java.util.concurrent.TimeUnit.SECONDS)) { proc.destroyForcibly(); return null }
                // Output: "New record inserted, URI: content://media/external/audio/media/123"
                Regex("URI:\\s*(content://\\S+)").find(output)?.groupValues?.get(1)?.let { Uri.parse(it) }
            } catch (_: Exception) { null }
        }

        private fun mediaScanAm(file: File) {
            val path = try { file.canonicalPath } catch (_: Exception) { file.absolutePath }
            try {
                ProcessBuilder(
                    "am", "broadcast",
                    "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                    "-d", "file://$path",
                    "--receiver-foreground"
                ).redirectErrorStream(true).start()
                    .waitFor(2, java.util.concurrent.TimeUnit.SECONDS)
            } catch (_: Exception) {}
        }

        private fun mediaScan(uri: Uri) {
            AndroidHiddenApi.broadcastIntent(
                Intent(Intent.ACTION_MEDIA_SCANNER_SCAN_FILE).apply { data = uri }
            )
        }
    }
}