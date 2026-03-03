package com.claudebot.app.speech

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalContext

/** State of the speech recognizer. */
enum class ListeningState { IDLE, LISTENING }

/** Abstraction over speech recognizer creation — injectable for testing. */
interface RecognizerFactory {
    fun isAvailable(): Boolean
    fun create(): RecognizerHandle?
}

/** Abstraction over a live recognizer instance. */
interface RecognizerHandle {
    fun setListener(listener: SpeechResultListener)
    fun startListening()
    fun stopListening()
    fun destroy()
}

/** Platform-agnostic callback interface for speech results. */
interface SpeechResultListener {
    fun onReadyForSpeech()
    fun onError(code: Int)
    fun onResults(text: String)
}

/**
 * Manages speech recognizer lifecycle and listening state.
 *
 * [factory] creates recognizer instances (injectable for testing).
 * [onResult] receives transcribed text when recognition succeeds.
 */
class SpeechRecognizerState(
    private val factory: RecognizerFactory,
    private val onResult: (String) -> Unit
) {
    var listeningState by mutableStateOf(ListeningState.IDLE)
        private set

    val isListening: Boolean get() = listeningState == ListeningState.LISTENING
    val isAvailable: Boolean get() = !destroyed && factory.isAvailable()

    private var handle: RecognizerHandle? = null
    private var destroyed = false

    internal val listener = object : SpeechResultListener {
        override fun onReadyForSpeech() {
            if (handle == null) return // already stopped/destroyed
            listeningState = ListeningState.LISTENING
        }

        override fun onError(code: Int) {
            if (handle == null) return // already stopped/destroyed
            listeningState = ListeningState.IDLE
            destroyHandle()
        }

        override fun onResults(text: String) {
            if (handle == null) return // already stopped/destroyed
            listeningState = ListeningState.IDLE
            if (text.isNotBlank()) onResult(text)
            destroyHandle()
        }
    }

    fun startListening() {
        if (destroyed || isListening || handle != null) return
        val h = factory.create() ?: return
        handle = h
        h.setListener(listener)
        h.startListening()
    }

    fun stopListening() {
        val h = handle
        handle = null // prevent listener callbacks from acting
        h?.stopListening()
        h?.destroy()
        listeningState = ListeningState.IDLE
    }

    fun destroy() {
        destroyed = true
        destroyHandle()
        listeningState = ListeningState.IDLE
    }

    private fun destroyHandle() {
        handle?.destroy()
        handle = null
    }
}

/** Android [SpeechRecognizer]-backed factory. */
class AndroidRecognizerFactory(private val context: Context) : RecognizerFactory {
    override fun isAvailable(): Boolean = SpeechRecognizer.isRecognitionAvailable(context)
    override fun create(): RecognizerHandle =
        AndroidRecognizerHandle(SpeechRecognizer.createSpeechRecognizer(context))
}

private class AndroidRecognizerHandle(
    private val recognizer: SpeechRecognizer
) : RecognizerHandle {
    override fun setListener(listener: SpeechResultListener) {
        recognizer.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) = listener.onReadyForSpeech()
            override fun onError(error: Int) = listener.onError(error)
            override fun onResults(results: Bundle?) {
                val matches = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                listener.onResults(matches?.firstOrNull() ?: "")
            }
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {}
            override fun onPartialResults(partialResults: Bundle?) {}
            override fun onEvent(eventType: Int, params: Bundle?) {}
        })
    }

    override fun startListening() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        }
        recognizer.startListening(intent)
    }

    override fun stopListening() = recognizer.stopListening()
    override fun destroy() = recognizer.destroy()
}

/** Create and remember a [SpeechRecognizerState] tied to the composition lifecycle. */
@Composable
fun rememberSpeechRecognizerState(
    onResult: (String) -> Unit,
): SpeechRecognizerState {
    val context = LocalContext.current
    val resultCallback = rememberUpdatedState(onResult)
    val state = remember {
        SpeechRecognizerState(
            factory = AndroidRecognizerFactory(context),
            onResult = { resultCallback.value(it) }
        )
    }
    DisposableEffect(Unit) {
        onDispose { state.destroy() }
    }
    return state
}
