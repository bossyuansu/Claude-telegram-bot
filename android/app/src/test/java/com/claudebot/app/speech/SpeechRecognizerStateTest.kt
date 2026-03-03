package com.claudebot.app.speech

import org.junit.Assert.*
import org.junit.Test

class SpeechRecognizerStateTest {

    @Test
    fun `initial state is IDLE`() {
        val state = createState()
        assertEquals(ListeningState.IDLE, state.listeningState)
        assertFalse(state.isListening)
    }

    @Test
    fun `startListening transitions to LISTENING on onReadyForSpeech`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        factory.lastHandle!!.triggerReady()
        assertEquals(ListeningState.LISTENING, state.listeningState)
        assertTrue(state.isListening)
    }

    @Test
    fun `results transition to IDLE and deliver text`() {
        val factory = FakeRecognizerFactory()
        var result = ""
        val state = createState(factory = factory, onResult = { result = it })
        state.startListening()
        factory.lastHandle!!.triggerReady()
        factory.lastHandle!!.triggerResults("hello world")
        assertEquals(ListeningState.IDLE, state.listeningState)
        assertEquals("hello world", result)
    }

    @Test
    fun `empty results do not call onResult`() {
        val factory = FakeRecognizerFactory()
        var resultCalled = false
        val state = createState(factory = factory, onResult = { resultCalled = true })
        state.startListening()
        factory.lastHandle!!.triggerReady()
        factory.lastHandle!!.triggerResults("")
        assertEquals(ListeningState.IDLE, state.listeningState)
        assertFalse(resultCalled)
    }

    @Test
    fun `error transitions to IDLE`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        factory.lastHandle!!.triggerReady()
        factory.lastHandle!!.triggerError(2)
        assertEquals(ListeningState.IDLE, state.listeningState)
        assertFalse(state.isListening)
    }

    @Test
    fun `error destroys handle`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        factory.lastHandle!!.triggerError(1)
        assertTrue(factory.lastHandle!!.destroyed)
    }

    @Test
    fun `stopListening transitions to IDLE and destroys handle`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        factory.lastHandle!!.triggerReady()
        assertTrue(state.isListening)
        state.stopListening()
        assertEquals(ListeningState.IDLE, state.listeningState)
        assertTrue(factory.lastHandle!!.stopped)
        assertTrue(factory.lastHandle!!.destroyed)
    }

    @Test
    fun `destroy cleans up handle`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        state.destroy()
        assertEquals(ListeningState.IDLE, state.listeningState)
        assertTrue(factory.lastHandle!!.destroyed)
    }

    @Test
    fun `startListening while already listening is no-op`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        factory.lastHandle!!.triggerReady()
        val firstHandle = factory.lastHandle
        state.startListening() // should be no-op
        assertSame(firstHandle, factory.lastHandle)
    }

    @Test
    fun `startListening after error creates new handle`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        val firstHandle = factory.lastHandle
        firstHandle!!.triggerError(1)
        // Now start again
        state.startListening()
        assertNotSame(firstHandle, factory.lastHandle)
    }

    @Test
    fun `isAvailable delegates to factory`() {
        val available = createState(factory = FakeRecognizerFactory(available = true))
        assertTrue(available.isAvailable)
        val unavailable = createState(factory = FakeRecognizerFactory(available = false))
        assertFalse(unavailable.isAvailable)
    }

    @Test
    fun `startListening after destroy is no-op`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        val firstHandle = factory.lastHandle
        state.destroy()
        state.startListening() // should be no-op — destroyed flag blocks
        assertSame(firstHandle, factory.lastHandle) // no new handle created
        assertFalse(state.isListening)
    }

    @Test
    fun `isAvailable returns false after destroy`() {
        val state = createState(factory = FakeRecognizerFactory(available = true))
        assertTrue(state.isAvailable)
        state.destroy()
        assertFalse(state.isAvailable)
    }

    @Test
    fun `rapid double startListening does not leak handle`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening() // creates handle, onReadyForSpeech not yet called
        val firstHandle = factory.lastHandle
        state.startListening() // handle != null, should be no-op
        assertSame(firstHandle, factory.lastHandle) // no second handle created
    }

    @Test
    fun `listener callbacks ignored after stopListening`() {
        val factory = FakeRecognizerFactory()
        var resultCalled = false
        val state = createState(factory = factory, onResult = { resultCalled = true })
        state.startListening()
        val handle = factory.lastHandle!!
        handle.triggerReady()
        state.stopListening()
        // Simulate stale onResults arriving after stop
        handle.listener!!.onResults("stale text")
        assertFalse(resultCalled) // should be ignored since handle was nulled
        assertEquals(ListeningState.IDLE, state.listeningState)
    }

    @Test
    fun `startListening when factory returns null is no-op`() {
        val factory = FakeRecognizerFactory(createReturnsNull = true)
        val state = createState(factory = factory)
        state.startListening()
        assertNull(factory.lastHandle)
        assertEquals(ListeningState.IDLE, state.listeningState)
    }

    @Test
    fun `stale onReadyForSpeech after stopListening does not re-enter LISTENING`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        val handle = factory.lastHandle!!
        handle.triggerReady()
        assertTrue(state.isListening)
        state.stopListening()
        assertEquals(ListeningState.IDLE, state.listeningState)
        // Simulate stale onReadyForSpeech arriving after stop
        handle.listener!!.onReadyForSpeech()
        assertEquals(ListeningState.IDLE, state.listeningState)
        assertFalse(state.isListening)
    }

    @Test
    fun `stale onError after stopListening is no-op`() {
        val factory = FakeRecognizerFactory()
        val state = createState(factory = factory)
        state.startListening()
        val handle = factory.lastHandle!!
        handle.triggerReady()
        state.stopListening()
        // Simulate stale onError arriving after stop
        handle.listener!!.onError(7)
        assertEquals(ListeningState.IDLE, state.listeningState)
    }

    @Test
    fun `listener callbacks ignored after destroy`() {
        val factory = FakeRecognizerFactory()
        var resultCalled = false
        val state = createState(factory = factory, onResult = { resultCalled = true })
        state.startListening()
        val handle = factory.lastHandle!!
        handle.triggerReady()
        state.destroy()
        // Simulate stale callback
        handle.listener!!.onResults("stale")
        assertFalse(resultCalled)
    }

    private fun createState(
        factory: RecognizerFactory = FakeRecognizerFactory(),
        onResult: (String) -> Unit = {}
    ) = SpeechRecognizerState(factory, onResult)
}

// --- Fakes for testing ---

class FakeRecognizerFactory(
    private val available: Boolean = true,
    private val createReturnsNull: Boolean = false
) : RecognizerFactory {
    var lastHandle: FakeRecognizerHandle? = null
        private set

    override fun isAvailable() = available

    override fun create(): RecognizerHandle? {
        if (createReturnsNull) return null
        val handle = FakeRecognizerHandle()
        lastHandle = handle
        return handle
    }
}

class FakeRecognizerHandle : RecognizerHandle {
    var listener: SpeechResultListener? = null
        private set
    var listening = false
        private set
    var stopped = false
        private set
    var destroyed = false
        private set

    override fun setListener(listener: SpeechResultListener) { this.listener = listener }
    override fun startListening() { listening = true }
    override fun stopListening() { stopped = true; listening = false }
    override fun destroy() { destroyed = true; listening = false }

    fun triggerReady() = listener!!.onReadyForSpeech()
    fun triggerResults(text: String) = listener!!.onResults(text)
    fun triggerError(code: Int) = listener!!.onError(code)
}
