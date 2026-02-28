package com.claudebot.app.ui.screens

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.claudebot.app.ChatViewModel
import com.claudebot.app.network.ConnectionState
import com.claudebot.app.ui.components.InputBar
import com.claudebot.app.ui.components.MessageBubble
import com.claudebot.app.ui.theme.*
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(viewModel: ChatViewModel) {
    val messages = viewModel.messages
    val connState by viewModel.connectionState
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()

    var showSearch by remember { mutableStateOf(false) }
    var showSessions by remember { mutableStateOf(false) }
    val searchQuery by viewModel.searchQuery
    val searchResults = viewModel.searchResults
    val isSearching by viewModel.isSearching
    val taskStatus by viewModel.taskStatus
    val isBusy by viewModel.isBotBusy

    // Persistent flag: is the user currently at/near the bottom?
    var stickToBottom by remember { mutableStateOf(true) }
    var hasNewMessages by remember { mutableStateOf(false) }

    // Monitor scroll position
    LaunchedEffect(listState) {
        snapshotFlow {
            val info = listState.layoutInfo
            if (info.totalItemsCount == 0) true
            else {
                val lastVisible = info.visibleItemsInfo.lastOrNull()?.index ?: 0
                lastVisible >= info.totalItemsCount - 3
            }
        }.distinctUntilChanged().collectLatest { atBottom ->
            stickToBottom = atBottom
            if (atBottom) hasNewMessages = false
        }
    }

    val scrollTrigger by viewModel.scrollTrigger
    LaunchedEffect(scrollTrigger) {
        if (scrollTrigger == 0) return@LaunchedEffect
        if (messages.isEmpty()) return@LaunchedEffect
        if (stickToBottom) {
            listState.animateScrollToItem(messages.size - 1)
        } else {
            hasNewMessages = true
        }
    }

    // Load more when scrolling near the top
    val isNearTop by remember {
        derivedStateOf { listState.firstVisibleItemIndex <= 3 }
    }
    val isLoadingMore by viewModel.isLoadingMore
    val allLoaded by viewModel.allLoaded

    LaunchedEffect(isNearTop) {
        if (isNearTop && !isLoadingMore && !allLoaded && messages.isNotEmpty()) {
            val sizeBefore = messages.size
            viewModel.loadMore()
            if (messages.size > sizeBefore) {
                val added = messages.size - sizeBefore
                listState.scrollToItem(listState.firstVisibleItemIndex + added)
            }
        }
    }

    Scaffold(
        modifier = Modifier.imePadding(),
        containerColor = DarkBg,
        topBar = {
            Column {
                if (showSearch) {
                    // Search bar
                    TopAppBar(
                        title = {
                            var text by remember { mutableStateOf(searchQuery) }
                            OutlinedTextField(
                                value = text,
                                onValueChange = { text = it },
                                modifier = Modifier.fillMaxWidth(),
                                placeholder = { Text("Search messages...", color = PlaceholderText, fontSize = 14.sp) },
                                singleLine = true,
                                shape = RoundedCornerShape(20.dp),
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = InputBorderFocused,
                                    unfocusedBorderColor = InputBorder,
                                    focusedTextColor = BotText,
                                    unfocusedTextColor = BotText,
                                    cursorColor = AccentOrange,
                                    focusedContainerColor = DarkSurfaceVariant,
                                    unfocusedContainerColor = DarkSurfaceVariant,
                                ),
                                trailingIcon = {
                                    if (text.isNotEmpty()) {
                                        Text(
                                            "\u2192",
                                            color = AccentOrange,
                                            modifier = Modifier.clickable { viewModel.search(text) }
                                        )
                                    }
                                }
                            )
                            LaunchedEffect(text) {
                                if (text.length >= 2) {
                                    kotlinx.coroutines.delay(400)
                                    viewModel.search(text)
                                } else if (text.isEmpty()) {
                                    viewModel.clearSearch()
                                }
                            }
                        },
                        colors = TopAppBarDefaults.topAppBarColors(containerColor = TopBarBg),
                        navigationIcon = {
                            IconButton(onClick = {
                                showSearch = false
                                viewModel.clearSearch()
                            }) {
                                Text("\u2190", fontSize = 20.sp, color = SessionLabel)
                            }
                        }
                    )
                } else {
                    TopAppBar(
                        title = {
                            val session by viewModel.currentSession
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                modifier = Modifier.clickable {
                                    showSessions = !showSessions
                                    if (showSessions) viewModel.fetchSessions()
                                }
                            ) {
                                val dotColor = when (connState) {
                                    ConnectionState.CONNECTED -> ConnectedGreen
                                    ConnectionState.RECONNECTING, ConnectionState.CONNECTING -> ReconnectingYellow
                                    ConnectionState.DISCONNECTED -> DisconnectedRed
                                }
                                Surface(
                                    modifier = Modifier.size(8.dp),
                                    shape = MaterialTheme.shapes.small,
                                    color = dotColor
                                ) {}
                                Spacer(Modifier.width(8.dp))
                                Column {
                                    Text(
                                        session.ifEmpty { "Claude Bot" },
                                        style = MaterialTheme.typography.titleMedium,
                                        color = TopBarTitle,
                                        maxLines = 1,
                                        overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis
                                    )
                                    if (taskStatus.active) {
                                        Text(
                                            "${taskStatus.mode} \u2022 ${taskStatus.phase}",
                                            fontSize = 11.sp,
                                            color = AccentOrangeLight,
                                            maxLines = 1
                                        )
                                    }
                                }
                                Text(if (showSessions) " \u25B2" else " \u25BC", fontSize = 10.sp, color = SessionLabel)
                            }
                        },
                        colors = TopAppBarDefaults.topAppBarColors(containerColor = TopBarBg),
                        actions = {
                            if (connState == ConnectionState.DISCONNECTED) {
                                TextButton(onClick = { viewModel.connect() }) {
                                    Text("Connect", fontSize = 12.sp, color = AccentOrange)
                                }
                            }
                            IconButton(onClick = { showSearch = true }) {
                                Text("\uD83D\uDD0D", fontSize = 16.sp)
                            }
                            IconButton(onClick = { viewModel.showSettings.value = true }) {
                                Text("\u2699", fontSize = 20.sp, color = SessionLabel)
                            }
                        }
                    )

                    // Session dropdown
                    AnimatedVisibility(visible = showSessions) {
                        Surface(color = DarkSurface) {
                            Column(modifier = Modifier.fillMaxWidth()) {
                                val sessions = viewModel.sessionList
                                if (sessions.isEmpty()) {
                                    Text(
                                        "No sessions",
                                        color = SessionLabel,
                                        fontSize = 13.sp,
                                        modifier = Modifier.padding(16.dp)
                                    )
                                } else {
                                    sessions.forEach { session ->
                                        Row(
                                            modifier = Modifier
                                                .fillMaxWidth()
                                                .clickable {
                                                    showSessions = false
                                                    if (!session.isActive) viewModel.switchSession(session.name)
                                                }
                                                .background(if (session.isActive) DarkSurfaceVariant else DarkSurface)
                                                .padding(horizontal = 16.dp, vertical = 10.dp),
                                            verticalAlignment = Alignment.CenterVertically
                                        ) {
                                            // Active indicator
                                            if (session.isActive) {
                                                Surface(
                                                    modifier = Modifier.size(6.dp),
                                                    shape = MaterialTheme.shapes.small,
                                                    color = AccentOrange
                                                ) {}
                                                Spacer(Modifier.width(8.dp))
                                            }
                                            Text(
                                                session.name,
                                                color = if (session.isActive) AccentOrange else BotText,
                                                fontSize = 14.sp,
                                                modifier = Modifier.weight(1f)
                                            )
                                            if (session.busy) {
                                                Text("\uD83D\uDD04", fontSize = 12.sp)
                                                Spacer(Modifier.width(4.dp))
                                            }
                                            Text(
                                                session.lastCli,
                                                color = SessionLabel,
                                                fontSize = 11.sp
                                            )
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

            }
        },
        bottomBar = {
            if (!showSearch) {
                val session by viewModel.currentSession
                val switching by viewModel.isSwitchingSession
                InputBar(
                    onSend = { viewModel.sendMessage(it) },
                    enabled = connState == ConnectionState.CONNECTED && !switching,
                    currentSession = if (switching) "Switching session..." else session
                )
            }
        }
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            if (showSearch && searchQuery.isNotEmpty()) {
                // Search results view
                if (searchResults.isEmpty() && !isSearching) {
                    Box(
                        modifier = Modifier.fillMaxSize(),
                        contentAlignment = Alignment.Center
                    ) {
                        Text("No results", color = SessionLabel, fontSize = 14.sp)
                    }
                } else {
                    LazyColumn(
                        modifier = Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(vertical = 8.dp)
                    ) {
                        items(searchResults, key = { "${it.messageId}_${it.timestamp}" }) { msg ->
                            MessageBubble(message = msg, onButtonClick = {})
                        }
                        if (viewModel.searchHasMore.value) {
                            item {
                                Box(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .padding(16.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    TextButton(onClick = { viewModel.searchMore() }) {
                                        Text("Load more", color = AccentOrange, fontSize = 12.sp)
                                    }
                                }
                            }
                        }
                    }
                }
                if (isSearching) {
                    LinearProgressIndicator(
                        modifier = Modifier.fillMaxWidth(),
                        color = AccentOrange,
                        trackColor = DarkSurface
                    )
                }
            } else if (!showSearch) {
                // Normal chat view
                if (messages.isEmpty()) {
                    Box(
                        modifier = Modifier.fillMaxSize(),
                        contentAlignment = Alignment.Center
                    ) {
                        val hint = when (connState) {
                            ConnectionState.CONNECTED -> "Connected. Send a message or /command."
                            ConnectionState.CONNECTING -> "Connecting..."
                            ConnectionState.RECONNECTING -> "Reconnecting..."
                            ConnectionState.DISCONNECTED -> "Disconnected. Tap Connect or check Settings."
                        }
                        Text(hint, color = SessionLabel, fontSize = 14.sp)
                    }
                } else {
                    LazyColumn(
                        state = listState,
                        modifier = Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(vertical = 8.dp)
                    ) {
                        if (isLoadingMore) {
                            item("loading_top") {
                                Box(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .padding(8.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    CircularProgressIndicator(
                                        modifier = Modifier.size(24.dp),
                                        color = AccentOrange,
                                        strokeWidth = 2.dp
                                    )
                                }
                            }
                        }
                        items(messages, key = { "${it.messageId}_${it.timestamp}" }) { msg ->
                            MessageBubble(
                                message = msg,
                                onButtonClick = { button ->
                                    viewModel.pressButton(msg.messageId, button)
                                }
                            )
                        }
                        // Typing indicator — shown as last item when bot is busy
                        if (isBusy) {
                            item("typing_indicator") {
                                TypingIndicator()
                            }
                        }
                    }
                }

                // New message indicator
                AnimatedVisibility(
                    visible = hasNewMessages,
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .padding(bottom = 8.dp),
                    enter = fadeIn() + slideInVertically { it },
                    exit = fadeOut() + slideOutVertically { it }
                ) {
                    FilledTonalButton(
                        onClick = {
                            hasNewMessages = false
                            scope.launch {
                                listState.animateScrollToItem(messages.size - 1)
                            }
                        },
                        shape = RoundedCornerShape(16.dp),
                        colors = ButtonDefaults.filledTonalButtonColors(
                            containerColor = DarkSurfaceVariant,
                            contentColor = AccentOrange
                        ),
                        contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp)
                    ) {
                        Text("\u2193 New messages", fontSize = 12.sp)
                    }
                }
            }
        }
    }
}

/** Animated typing indicator — three pulsing dots in a bot-style bubble. */
@Composable
private fun TypingIndicator() {
    val infiniteTransition = rememberInfiniteTransition(label = "typing")

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 3.dp),
        horizontalArrangement = Arrangement.Start
    ) {
        Row(
            modifier = Modifier
                .clip(RoundedCornerShape(16.dp, 16.dp, 16.dp, 4.dp))
                .border(1.dp, BotBubbleBorder, RoundedCornerShape(16.dp, 16.dp, 16.dp, 4.dp))
                .background(BotBubble)
                .padding(horizontal = 14.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(4.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            repeat(3) { i ->
                val alpha by infiniteTransition.animateFloat(
                    initialValue = 0.3f,
                    targetValue = 1f,
                    animationSpec = infiniteRepeatable(
                        animation = keyframes {
                            durationMillis = 1200
                            0.3f at 0
                            1f at 300
                            0.3f at 600
                            0.3f at 1200
                        },
                        repeatMode = RepeatMode.Restart,
                        initialStartOffset = StartOffset(i * 200)
                    ),
                    label = "dot$i"
                )
                Box(
                    modifier = Modifier
                        .size(7.dp)
                        .clip(CircleShape)
                        .background(AccentOrange.copy(alpha = alpha))
                )
            }
        }
    }
}
