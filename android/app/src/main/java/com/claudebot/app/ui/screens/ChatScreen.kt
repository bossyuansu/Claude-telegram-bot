package com.claudebot.app.ui.screens

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.gestures.scrollBy
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
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
    var showOutline by remember { mutableStateOf(false) }
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = false)
    val searchQuery by viewModel.searchQuery
    val searchResults = viewModel.searchResults
    val isSearching by viewModel.isSearching
    val taskStatus by viewModel.taskStatus
    val isBusy by viewModel.isBotBusy

    // Back-to-exit confirmation: only intercept when NOT already primed
    var backPressedOnce by remember { mutableStateOf(false) }
    val snackbarHostState = remember { SnackbarHostState() }
    BackHandler(enabled = !backPressedOnce) {
        backPressedOnce = true
        scope.launch {
            snackbarHostState.showSnackbar(
                message = "Swipe again to close",
                duration = SnackbarDuration.Short
            )
            delay(2000)
            backPressedOnce = false
        }
    }

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

    // Scroll to bottom on first load
    LaunchedEffect(Unit) {
        snapshotFlow { messages.size }
            .distinctUntilChanged()
            .collectLatest { size ->
                if (size > 0) {
                    listState.scrollToItem(size - 1)
                    return@collectLatest
                }
            }
    }

    val scrollTrigger by viewModel.scrollTrigger
    LaunchedEffect(scrollTrigger) {
        if (scrollTrigger == 0) return@LaunchedEffect
        if (messages.isEmpty()) return@LaunchedEffect
        if (stickToBottom) {
            listState.scrollToItem(messages.size - 1)
            listState.scrollBy(Float.MAX_VALUE)
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
        snackbarHost = { SnackbarHost(snackbarHostState) },
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
                            IconButton(onClick = { showOutline = true }) {
                                Text("📑", fontSize = 16.sp)
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
                val action by viewModel.pendingAction
                Column {
                    // Sticky action bar for pending questions/approvals
                    if (action != null) {
                        Surface(
                            color = DarkSurface,
                            shadowElevation = 8.dp,
                        ) {
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(12.dp)
                            ) {
                                // "View plan" tap to scroll to the plan message
                                Text(
                                    text = "▲ Tap to view full message",
                                    color = AccentOrangeLight,
                                    fontSize = 11.sp,
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .clickable {
                                            val mid = action!!.messageId
                                            val idx = messages.indexOfFirst { it.messageId == mid }
                                            if (idx >= 0) {
                                                scope.launch { listState.animateScrollToItem(idx) }
                                            }
                                        }
                                        .padding(bottom = 6.dp)
                                )
                                // Question text — scrollable for long plans
                                Text(
                                    text = action!!.text,
                                    color = BotText,
                                    fontSize = 13.sp,
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .heightIn(max = 120.dp)
                                        .verticalScroll(rememberScrollState())
                                )
                                Spacer(Modifier.height(8.dp))
                                // Action buttons
                                Row(
                                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                                    modifier = Modifier.fillMaxWidth()
                                ) {
                                    action!!.buttons.flatten().forEach { btn ->
                                        val isApprove = btn.text.contains("Approve", ignoreCase = true)
                                        val isReject = btn.text.contains("Reject", ignoreCase = true)
                                        if (isApprove) {
                                            Button(
                                                onClick = { viewModel.pressButton(action!!.messageId, btn) },
                                                modifier = Modifier.weight(1f),
                                                colors = ButtonDefaults.buttonColors(
                                                    containerColor = ConnectedGreen,
                                                    contentColor = UserBubbleText,
                                                ),
                                                shape = RoundedCornerShape(8.dp)
                                            ) {
                                                Text(btn.text, fontSize = 14.sp)
                                            }
                                        } else if (isReject) {
                                            OutlinedButton(
                                                onClick = { viewModel.pressButton(action!!.messageId, btn) },
                                                modifier = Modifier.weight(1f),
                                                border = BorderStroke(1.dp, DisconnectedRed),
                                                colors = ButtonDefaults.outlinedButtonColors(
                                                    contentColor = DisconnectedRed,
                                                ),
                                                shape = RoundedCornerShape(8.dp)
                                            ) {
                                                Text(btn.text, fontSize = 14.sp)
                                            }
                                        } else {
                                            OutlinedButton(
                                                onClick = { viewModel.pressButton(action!!.messageId, btn) },
                                                modifier = Modifier.weight(1f),
                                                border = BorderStroke(1.dp, AccentOrange),
                                                colors = ButtonDefaults.outlinedButtonColors(
                                                    contentColor = AccentOrange,
                                                ),
                                                shape = RoundedCornerShape(8.dp)
                                            ) {
                                                Text(btn.text, fontSize = 14.sp, maxLines = 1)
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    InputBar(
                        onSend = { viewModel.sendMessage(it) },
                        enabled = connState == ConnectionState.CONNECTED && !switching,
                        currentSession = if (switching) "Switching session..." else session,
                        isBusy = isBusy,
                        onCancel = { viewModel.sendMessage("/cancel") }
                    )
                }
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
                        items(messages.size, key = { i -> "${messages[i].messageId}_${messages[i].timestamp}_$i" }) { i ->
                            val msg = messages[i]
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

                // Scroll-to-bottom button (shows when scrolled up)
                AnimatedVisibility(
                    visible = !stickToBottom,
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
                        Text(
                            if (hasNewMessages) "\u2193 New messages" else "\u2193 Bottom",
                            fontSize = 12.sp
                        )
                    }
                }
            }
        }

        if (showOutline) {
            ModalBottomSheet(
                onDismissRequest = { showOutline = false },
                sheetState = sheetState,
                containerColor = DarkSurface
            ) {
                val userPrompts = remember(messages) {
                    messages.mapIndexedNotNull { index, msg ->
                        if (!msg.isFromBot && msg.text.isNotBlank()) index to msg else null
                    }.reversed()
                }

                Text(
                    "Session Outline",
                    style = MaterialTheme.typography.titleMedium,
                    color = TopBarTitle,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)
                )

                if (userPrompts.isEmpty()) {
                    Text(
                        "No prompts yet.",
                        color = SessionLabel,
                        fontSize = 14.sp,
                        modifier = Modifier.padding(16.dp)
                    )
                } else {
                    LazyColumn(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(max = 400.dp)
                    ) {
                        items(userPrompts) { (originalIndex, msg) ->
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable {
                                        scope.launch {
                                            showOutline = false
                                            sheetState.hide()
                                            listState.animateScrollToItem(originalIndex)
                                        }
                                    }
                                    .padding(horizontal = 16.dp, vertical = 12.dp)
                            ) {
                                Text(
                                    text = msg.text,
                                    color = BotText,
                                    maxLines = 2,
                                    overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis,
                                    fontSize = 14.sp
                                )
                                Spacer(Modifier.height(4.dp))
                                Text(
                                    text = java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault()).format(java.util.Date(msg.timestamp)),
                                    color = SessionLabel,
                                    fontSize = 10.sp
                                )
                            }
                            HorizontalDivider(color = InputBorder)
                        }
                    }
                }
                Spacer(Modifier.height(WindowInsets.navigationBars.asPaddingValues().calculateBottomPadding()))
            }
        }
    }
}

/** Animated typing indicator — three pulsing dots in a bot-style bubble.
 *  Uses a manual coroutine loop instead of rememberInfiniteTransition
 *  to avoid continuous recomposition that prevents screen timeout. */
@Composable
private fun TypingIndicator() {
    // Cycle through which dot is "active" (0, 1, 2) every 200ms
    var activeDot by remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) {
        while (true) {
            kotlinx.coroutines.delay(200)
            activeDot = (activeDot + 1) % 3
        }
    }

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
                val alpha = if (i == activeDot) 1f else 0.3f
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
