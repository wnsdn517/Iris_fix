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
            val t0 = System.currentTimeMillis()
            System.err.println("[Iris/timing] sendFileInternal: start file=$filePath room=$room")
            val file = File(filePath)
            val mimeType = getMimeType(file.extension.lowercase())
            System.err.println("[Iris/timing] sendFileInternal: ext=${file.extension} mimeType=$mimeType")

            val uri = Uri.fromFile(file)
            System.err.println("[Iris/timing] sendFileInternal: uri=$uri")

            val flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP

            val isMultiple = mimeType.startsWith("audio/") || mimeType.startsWith("text/")
            val intent = if (isMultiple) {
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
            System.err.println("[Iris/timing] sendFileInternal: intent=${if (isMultiple) "ACTION_SEND_MULTIPLE" else "ACTION_SEND"} type=${intent.type}")
            val t1 = System.currentTimeMillis()
            try {
                AndroidHiddenApi.startActivity(intent)
                System.err.println("[Iris/timing] sendFileInternal: startActivity took ${System.currentTimeMillis()-t1}ms → OK (total ${System.currentTimeMillis()-t0}ms)")
            } catch (e: Exception) {
                System.err.println("[Iris/timing] sendFileInternal: startActivity took ${System.currentTimeMillis()-t1}ms → FAIL: $e (total ${System.currentTimeMillis()-t0}ms)")
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
            val uri = Uri.fromFile(file)

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
                    Intent.FLAG_ACTIVITY_CLEAR_TOP
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
            val t0 = System.currentTimeMillis()
            System.err.println("[Iris/timing] sendMultiplePhotos: start count=${base64ImageDataStrings.size} room=$room")
            val picDir = File(IMAGE_DIR_PATH).apply { if (!exists()) mkdirs() }

            val uris = base64ImageDataStrings.mapIndexed { idx, base64 ->
                val t = System.currentTimeMillis()
                val imageFile = File(picDir, "${System.currentTimeMillis()}_${idx}.png").apply {
                    writeBytes(Base64.decode(base64, Base64.DEFAULT))
                }
                val uri = Uri.fromFile(imageFile)
                System.err.println("[Iris/timing] sendMultiplePhotos: wrote image[$idx] ${imageFile.length()}B in ${System.currentTimeMillis()-t}ms → $uri")
                uri
            }

            if (uris.isEmpty()) {
                System.err.println("[Iris/timing] sendMultiplePhotos: no URIs created, abort")
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

            val t1 = System.currentTimeMillis()
            try {
                AndroidHiddenApi.startActivity(intent)
                System.err.println("[Iris/timing] sendMultiplePhotos: startActivity took ${System.currentTimeMillis()-t1}ms → OK (total ${System.currentTimeMillis()-t0}ms)")
            } catch (e: Exception) {
                System.err.println("[Iris/timing] sendMultiplePhotos: startActivity took ${System.currentTimeMillis()-t1}ms → FAIL: $e (total ${System.currentTimeMillis()-t0}ms)")
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
            val t0 = System.currentTimeMillis()
            System.err.println("[Iris/timing] getContentUri: start file=${file.absolutePath}")
            val appCtx = try {
                Class.forName("android.app.ActivityThread")
                    .getMethod("currentApplication").invoke(null) as? android.content.Context
            } catch (_: Exception) { null } ?: try {
                Class.forName("android.app.AppGlobals")
                    .getMethod("getInitialApplication").invoke(null) as? android.content.Context
            } catch (_: Exception) { null }

            if (appCtx != null) {
                System.err.println("[Iris/timing] getContentUri: appCtx available, trying FileProvider")
                return try {
                    val uri = FileProvider.getUriForFile(appCtx, "${appCtx.packageName}.fileprovider", file)
                    System.err.println("[Iris/timing] getContentUri: FileProvider → $uri (${System.currentTimeMillis()-t0}ms)")
                    uri
                } catch (e: Exception) {
                    System.err.println("[Iris/timing] getContentUri: FileProvider failed ($e), falling to queryOrFallbackUri")
                    queryOrFallbackUri(file).also {
                        System.err.println("[Iris/timing] getContentUri: → $it (total ${System.currentTimeMillis()-t0}ms)")
                    }
                }
            }
            System.err.println("[Iris/timing] getContentUri: no appCtx (app_process), using queryOrFallbackUri")
            return queryOrFallbackUri(file).also {
                System.err.println("[Iris/timing] getContentUri: → $it (total ${System.currentTimeMillis()-t0}ms)")
            }
        }

        private fun queryOrFallbackUri(file: File): Uri {
            val t0 = System.currentTimeMillis()
            System.err.println("[Iris/timing] queryOrFallbackUri: start file=${file.absolutePath}")

            // 1. Already indexed in MediaStore?
            var t = System.currentTimeMillis()
            queryMediaStoreUri(file)?.let {
                System.err.println("[Iris/timing] queryOrFallbackUri: step1/query took ${System.currentTimeMillis()-t}ms → HIT $it (total ${System.currentTimeMillis()-t0}ms)")
                return it
            }
            System.err.println("[Iris/timing] queryOrFallbackUri: step1/query took ${System.currentTimeMillis()-t}ms → miss")

            // 2. Synchronous scan (Android 11+/API 30+): returns content:// URI immediately
            t = System.currentTimeMillis()
            scanAndGetUri(file)?.let {
                System.err.println("[Iris/timing] queryOrFallbackUri: step2/scan took ${System.currentTimeMillis()-t}ms → HIT $it (total ${System.currentTimeMillis()-t0}ms)")
                return it
            }
            System.err.println("[Iris/timing] queryOrFallbackUri: step2/scan took ${System.currentTimeMillis()-t}ms → miss")

            // 3. Direct insert into MediaStore
            t = System.currentTimeMillis()
            insertMediaStoreEntry(file)?.let {
                System.err.println("[Iris/timing] queryOrFallbackUri: step3/insert took ${System.currentTimeMillis()-t}ms → HIT $it (total ${System.currentTimeMillis()-t0}ms)")
                return it
            }
            System.err.println("[Iris/timing] queryOrFallbackUri: step3/insert took ${System.currentTimeMillis()-t}ms → miss")

            // 4. Force async scan, poll for up to ~2s
            t = System.currentTimeMillis()
            mediaScanAm(file)
            System.err.println("[Iris/timing] queryOrFallbackUri: step4/mediaScanAm took ${System.currentTimeMillis()-t}ms")
            for ((idx, delayMs) in longArrayOf(300L, 600L, 1000L).withIndex()) {
                Thread.sleep(delayMs)
                val tPoll = System.currentTimeMillis()
                queryMediaStoreUri(file)?.let {
                    System.err.println("[Iris/timing] queryOrFallbackUri: step4/poll[$idx] sleep=${delayMs}ms query took ${System.currentTimeMillis()-tPoll}ms → HIT $it (total ${System.currentTimeMillis()-t0}ms)")
                    return it
                }
                System.err.println("[Iris/timing] queryOrFallbackUri: step4/poll[$idx] sleep=${delayMs}ms query took ${System.currentTimeMillis()-tPoll}ms → miss")
            }

            val fallback = Uri.fromFile(file)
            System.err.println("[Iris/timing] queryOrFallbackUri: step5/fallback → $fallback (total ${System.currentTimeMillis()-t0}ms)")
            return fallback
        }

        private fun queryMediaStoreUri(file: File): Uri? {
            val t0 = System.currentTimeMillis()
            val tableUri = mediaTableUri(file.extension.lowercase()) ?: run {
                System.err.println("[Iris/timing] queryMediaStoreUri: no table for ext=${file.extension} → null")
                return null
            }
            val paths = listOfNotNull(
                try { file.canonicalPath } catch (_: Exception) { null },
                file.absolutePath
            ).distinct()

            // In-process via ContentResolver (fastest, no shell spawn)
            systemContext?.contentResolver?.let { resolver ->
                for (path in paths) {
                    val tq = System.currentTimeMillis()
                    val cursor = resolver.query(
                        tableUri,
                        arrayOf(MediaStore.MediaColumns._ID),
                        "${MediaStore.MediaColumns.DATA} = ?",
                        arrayOf(path), null
                    ) ?: continue
                    cursor.use {
                        if (it.moveToFirst()) {
                            val uri = ContentUris.withAppendedId(tableUri, it.getLong(0))
                            System.err.println("[Iris/timing] queryMediaStoreUri: resolver.query took ${System.currentTimeMillis()-tq}ms → HIT $uri")
                            return uri
                        }
                    }
                    System.err.println("[Iris/timing] queryMediaStoreUri: resolver.query took ${System.currentTimeMillis()-tq}ms → miss for $path")
                }
                System.err.println("[Iris/timing] queryMediaStoreUri: resolver all miss (${System.currentTimeMillis()-t0}ms)")
                return null
            }

            // Fallback: shell `content query`
            System.err.println("[Iris/timing] queryMediaStoreUri: systemContext=null, shell fallback")
            val sub = when (tableUri) {
                MediaStore.Audio.Media.EXTERNAL_CONTENT_URI -> "audio/media"
                MediaStore.Video.Media.EXTERNAL_CONTENT_URI -> "video/media"
                else -> "images/media"
            }
            for (path in paths) {
                val ts = System.currentTimeMillis()
                runMediaStoreQuery("content://media/external/$sub", path)?.let {
                    System.err.println("[Iris/timing] queryMediaStoreUri: shell.query took ${System.currentTimeMillis()-ts}ms → HIT $it")
                    return it
                }
                System.err.println("[Iris/timing] queryMediaStoreUri: shell.query took ${System.currentTimeMillis()-ts}ms → miss for $path")
            }
            System.err.println("[Iris/timing] queryMediaStoreUri: shell all miss (${System.currentTimeMillis()-t0}ms)")
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
            val t0 = System.currentTimeMillis()
            val path = try { file.canonicalPath } catch (_: Exception) { file.absolutePath }

            // In-process via ContentResolver.call (what `content call` runs internally)
            systemContext?.contentResolver?.let { resolver ->
                return try {
                    val t = System.currentTimeMillis()
                    @Suppress("DEPRECATION")
                    val result = resolver.call(Uri.parse("content://media"), "scan_file", path, null)
                        ?.getParcelable<Uri>("uri")
                    System.err.println("[Iris/timing] scanAndGetUri: resolver.call took ${System.currentTimeMillis()-t}ms → ${result ?: "null"}")
                    result
                } catch (e: Exception) {
                    System.err.println("[Iris/timing] scanAndGetUri: resolver.call failed in ${System.currentTimeMillis()-t0}ms: $e")
                    null
                }
            }

            // Fallback: shell `content call --method scan_file`
            System.err.println("[Iris/timing] scanAndGetUri: systemContext=null, shell fallback")
            return try {
                val t = System.currentTimeMillis()
                val proc = ProcessBuilder(
                    "content", "call",
                    "--uri", "content://media",
                    "--method", "scan_file",
                    "--arg", path
                ).redirectErrorStream(true).start()
                val output = proc.inputStream.bufferedReader().readText()
                if (!proc.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) {
                    proc.destroyForcibly()
                    System.err.println("[Iris/timing] scanAndGetUri: shell.call timed out after ${System.currentTimeMillis()-t}ms")
                    return null
                }
                // Result: Bundle[{uri=content://media/external/audio/media/123}]
                val result = Regex("uri=(content://[^}\\s,]+)").find(output)?.groupValues?.get(1)?.let { Uri.parse(it) }
                System.err.println("[Iris/timing] scanAndGetUri: shell.call took ${System.currentTimeMillis()-t}ms → ${result ?: "null"}")
                result
            } catch (e: Exception) {
                System.err.println("[Iris/timing] scanAndGetUri: shell.call failed: $e")
                null
            }
        }

        private fun insertMediaStoreEntry(file: File): Uri? {
            val t0 = System.currentTimeMillis()
            val mimeType = getMimeType(file.extension.lowercase())
            val tableUri = when {
                mimeType.startsWith("audio/") -> MediaStore.Audio.Media.EXTERNAL_CONTENT_URI
                mimeType.startsWith("video/") -> MediaStore.Video.Media.EXTERNAL_CONTENT_URI
                else -> {
                    System.err.println("[Iris/timing] insertMediaStoreEntry: skipped for mimeType=$mimeType → null")
                    return null
                }
            }
            val path = try { file.canonicalPath } catch (_: Exception) { file.absolutePath }

            // In-process via ContentResolver.insert
            systemContext?.contentResolver?.let { resolver ->
                return try {
                    val t = System.currentTimeMillis()
                    val result = resolver.insert(tableUri, ContentValues().apply {
                        put(MediaStore.MediaColumns.DATA, path)
                        put(MediaStore.MediaColumns.DISPLAY_NAME, file.name)
                        put(MediaStore.MediaColumns.MIME_TYPE, mimeType)
                    })
                    System.err.println("[Iris/timing] insertMediaStoreEntry: resolver.insert took ${System.currentTimeMillis()-t}ms → ${result ?: "null"}")
                    result
                } catch (e: Exception) {
                    System.err.println("[Iris/timing] insertMediaStoreEntry: resolver.insert failed in ${System.currentTimeMillis()-t0}ms: $e")
                    null
                }
            }

            // Fallback: shell `content insert`
            System.err.println("[Iris/timing] insertMediaStoreEntry: systemContext=null, shell fallback")
            val sub = if (mimeType.startsWith("audio/")) "audio/media" else "video/media"
            for (volume in listOf("external", "external_primary")) {
                val t = System.currentTimeMillis()
                runMediaStoreInsert("content://media/$volume/$sub", path, file.name, mimeType)?.let {
                    System.err.println("[Iris/timing] insertMediaStoreEntry: shell.insert[$volume] took ${System.currentTimeMillis()-t}ms → HIT $it")
                    return it
                }
                System.err.println("[Iris/timing] insertMediaStoreEntry: shell.insert[$volume] took ${System.currentTimeMillis()-t}ms → miss")
            }
            System.err.println("[Iris/timing] insertMediaStoreEntry: all miss (${System.currentTimeMillis()-t0}ms)")
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
            val t = System.currentTimeMillis()
            try {
                ProcessBuilder(
                    "am", "broadcast",
                    "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                    "-d", "file://$path",
                    "--receiver-foreground"
                ).redirectErrorStream(true).start()
                    .waitFor(2, java.util.concurrent.TimeUnit.SECONDS)
                System.err.println("[Iris/timing] mediaScanAm: broadcast took ${System.currentTimeMillis()-t}ms")
            } catch (e: Exception) {
                System.err.println("[Iris/timing] mediaScanAm: broadcast failed in ${System.currentTimeMillis()-t}ms: $e")
            }
        }

        private fun mediaScan(uri: Uri) {
            AndroidHiddenApi.broadcastIntent(
                Intent(Intent.ACTION_MEDIA_SCANNER_SCAN_FILE).apply { data = uri }
            )
        }
    }
}