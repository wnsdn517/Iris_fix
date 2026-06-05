package party.qwer.iris

import android.app.RemoteInput
import android.content.ComponentName
import android.content.Intent
import android.net.Uri
import android.os.Bundle
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

            // text/* files must be sent via ACTION_SEND_MULTIPLE + */* to be received correctly
            val intent = if (mimeType.startsWith("text/")) {
                Intent(Intent.ACTION_SEND_MULTIPLE).apply {
                    setPackage("com.kakao.talk")
                    type = "*/*"
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
            val picDir = File(IMAGE_DIR_PATH).apply {
                if (!exists()) {
                    mkdirs()
                }
            }

            val uris = base64ImageDataStrings.mapIndexed { idx, base64ImageDataString ->
                val decodedImage = Base64.decode(base64ImageDataString, Base64.DEFAULT)
                val timestamp = System.currentTimeMillis().toString()

                val imageFile = File(picDir, "${timestamp}_${idx}.png").apply {
                    writeBytes(decodedImage)
                }

                mediaScan(Uri.fromFile(imageFile))
                getContentUri(imageFile)
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
                addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                    Intent.FLAG_ACTIVITY_CLEAR_TOP or
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                )
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

        private fun getContentUri(file: File): Uri {
            val context = try {
                Class.forName("android.app.ActivityThread")
                    .getMethod("currentApplication")
                    .invoke(null) as? android.content.Context
            } catch (_: Exception) { null } ?: try {
                Class.forName("android.app.AppGlobals")
                    .getMethod("getInitialApplication")
                    .invoke(null) as? android.content.Context
            } catch (_: Exception) { null }

            return if (context != null) {
                try {
                    FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
                } catch (_: Exception) {
                    Uri.fromFile(file)
                }
            } else {
                Uri.fromFile(file)
            }
        }

        private fun mediaScan(uri: Uri) {
            val mediaScanIntent = Intent(Intent.ACTION_MEDIA_SCANNER_SCAN_FILE).apply {
                data = uri
            }
            AndroidHiddenApi.broadcastIntent(mediaScanIntent)
        }
    }
}