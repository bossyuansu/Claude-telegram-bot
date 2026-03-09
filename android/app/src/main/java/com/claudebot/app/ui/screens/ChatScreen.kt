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
import androidx.compose.foundation.lazy.LazyRow
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.zIndex
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
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
    var showFilterMenu by remember { mutableStateOf(false) }
    var showToolsMenu by remember { mutableStateOf(false) }
    var showMissionControl by remember { mutableStateOf(false) }
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
    var newMessageCount by remember { mutableIntStateOf(0) }

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
            if (atBottom) newMessageCount = 0
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
            newMessageCount++
        }
    }

    // Load more when scrolling near the top
    val isNearTop by remember {
        derivedStateOf { listState.firstVisibleItemIndex <= 3 }
    }
    val isLoadingMore by viewModel.isLoadingMore
    val allLoaded by viewModel.allLoaded

    // Track message count before loadMore so we can adjust scroll after prepend
    var sizeBeforeLoad by remember { mutableIntStateOf(0) }

    LaunchedEffect(isNearTop) {
        if (isNearTop && !isLoadingMore && !allLoaded && messages.isNotEmpty()) {
            sizeBeforeLoad = messages.size
            viewModel.loadMore()
        }
    }

    // Adjust scroll position after loadMore completes (isLoadingMore: true -> false)
    LaunchedEffect(isLoadingMore) {
        if (!isLoadingMore && sizeBeforeLoad > 0 && messages.size > sizeBeforeLoad) {
            val added = messages.size - sizeBeforeLoad
            listState.scrollToItem(listState.firstVisibleItemIndex + added)
            sizeBeforeLoad = 0
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
                            val serverSession by viewModel.currentSession
                            val filterSession by viewModel.sessionFilter
                            val session = filterSession ?: serverSession
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                modifier = Modifier.clickable {
                                    showSessions = !showSessions
                                    if (showSessions) viewModel.fetchSessions()
                                }
                            ) {
                                val baseDotColor = when (connState) {
                                    ConnectionState.CONNECTED -> ConnectedGreen
                                    ConnectionState.RECONNECTING, ConnectionState.CONNECTING -> ReconnectingYellow
                                    ConnectionState.DISCONNECTED -> DisconnectedRed
                                }
                                val dotPulsing = connState == ConnectionState.RECONNECTING || connState == ConnectionState.CONNECTING
                                if (dotPulsing) {
                                    val dotTransition = rememberInfiniteTransition(label = "dotPulse")
                                    val pulseAlpha by dotTransition.animateFloat(
                                        initialValue = 0.4f,
                                        targetValue = 1f,
                                        animationSpec = infiniteRepeatable(
                                            animation = tween(800),
                                            repeatMode = RepeatMode.Reverse
                                        ),
                                        label = "dotAlpha"
                                    )
                                    Surface(
                                        modifier = Modifier.size(8.dp),
                                        shape = MaterialTheme.shapes.small,
                                        color = baseDotColor.copy(alpha = pulseAlpha)
                                    ) {}
                                } else {
                                    Surface(
                                        modifier = Modifier.size(8.dp),
                                        shape = MaterialTheme.shapes.small,
                                        color = baseDotColor
                                    ) {}
                                }
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
                            // Compact session filter dropdown
                            val availableSessions = viewModel.availableSessions
                            val sessionFilter by viewModel.sessionFilter
                            if (availableSessions.size > 1) {
                                SessionFilterButton(
                                    expanded = showFilterMenu,
                                    onToggle = { showFilterMenu = !showFilterMenu },
                                    onDismiss = { showFilterMenu = false },
                                    sessions = availableSessions,
                                    activeFilter = sessionFilter,
                                    onSelect = { viewModel.setSessionFilter(it); showFilterMenu = false }
                                )
                            }
                            // Mission Control badge
                            val activeTaskCount = viewModel.activeTasks.size
                            Box {
                                IconButton(onClick = {
                                    showMissionControl = true
                                    viewModel.fetchActiveTasks()
                                }) {
                                    Text("\u26A1", fontSize = 16.sp)
                                }
                                if (activeTaskCount > 0) {
                                    Surface(
                                        modifier = Modifier
                                            .size(16.dp)
                                            .align(Alignment.TopEnd)
                                            .offset(x = (-2).dp, y = 6.dp),
                                        shape = CircleShape,
                                        color = AccentOrange
                                    ) {
                                        Box(contentAlignment = Alignment.Center) {
                                            Text(
                                                "$activeTaskCount",
                                                fontSize = 9.sp,
                                                color = DarkBg,
                                                lineHeight = 9.sp,
                                                modifier = Modifier.offset(y = (-1).dp)
                                            )
                                        }
                                    }
                                }
                            }
                            Box {
                                IconButton(onClick = { showToolsMenu = !showToolsMenu }) {
                                    Text("\u22EF", fontSize = 20.sp, color = SessionLabel)
                                }
                                MaterialTheme(colorScheme = MaterialTheme.colorScheme.copy(surface = DarkSurface)) {
                                DropdownMenu(
                                    expanded = showToolsMenu,
                                    onDismissRequest = { showToolsMenu = false },
                                ) {
                                    DropdownMenuItem(
                                        text = { Text("Search", fontSize = 13.sp, color = BotText) },
                                        onClick = { showToolsMenu = false; showSearch = true },
                                        leadingIcon = { Text("\uD83D\uDD0D", fontSize = 14.sp) }
                                    )
                                    DropdownMenuItem(
                                        text = { Text("Outline", fontSize = 13.sp, color = BotText) },
                                        onClick = { showToolsMenu = false; showOutline = true },
                                        leadingIcon = { Text("📑", fontSize = 14.sp) }
                                    )
                                    DropdownMenuItem(
                                        text = { Text("Settings", fontSize = 13.sp, color = BotText) },
                                        onClick = { showToolsMenu = false; viewModel.showSettings.value = true },
                                        leadingIcon = { Text("\u2699", fontSize = 14.sp) }
                                    )
                                }
                                }
                            }
                        }
                    )

                    // Session dropdown with stagger animation
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
                                    sessions.forEachIndexed { index, session ->
                                        var rowVisible by remember { mutableStateOf(false) }
                                        LaunchedEffect(Unit) {
                                            delay(index * 40L)
                                            rowVisible = true
                                        }
                                        AnimatedVisibility(
                                            visible = rowVisible,
                                            enter = fadeIn(tween(150)) + slideInVertically(tween(150)) { -it / 2 }
                                        ) {
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

            }
        },
        bottomBar = {
            if (!showSearch) {
                val session by viewModel.currentSession
                val switching by viewModel.isSwitchingSession
                val action by viewModel.pendingAction
                Column {
                    // Sticky action bar for pending questions/approvals
                    val currentAction = action
                    if (currentAction != null) {
                        Surface(
                            color = DarkSurface,
                            shadowElevation = 8.dp,
                        ) {
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(12.dp)
                            ) {
                                // Top row: "View full message" + Dismiss
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.SpaceBetween,
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Text(
                                        text = "▲ Tap to view full message",
                                        color = AccentOrangeLight,
                                        fontSize = 11.sp,
                                        modifier = Modifier
                                            .clickable {
                                                val idx = messages.indexOfFirst { it.messageId == currentAction.messageId }
                                                if (idx >= 0) {
                                                    scope.launch { listState.animateScrollToItem(idx) }
                                                }
                                            }
                                            .padding(bottom = 6.dp)
                                    )
                                    Text(
                                        text = "Dismiss",
                                        color = Color.Gray,
                                        fontSize = 11.sp,
                                        modifier = Modifier
                                            .clickable { viewModel.dismissPendingAction() }
                                            .padding(bottom = 6.dp)
                                    )
                                }
                                // Question text — scrollable for long plans
                                Text(
                                    text = currentAction.text,
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
                                    currentAction.buttons.flatten().forEach { btn ->
                                        val isApprove = btn.text.contains("Approve", ignoreCase = true)
                                        val isReject = btn.text.contains("Reject", ignoreCase = true)
                                        if (isApprove) {
                                            Button(
                                                onClick = { viewModel.pressButton(currentAction.messageId, btn) },
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
                                                onClick = { viewModel.pressButton(currentAction.messageId, btn) },
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
                                                onClick = { viewModel.pressButton(currentAction.messageId, btn) },
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
                // Session mismatch banner (viewing another session's chat via MC)
                val viewingOther = viewModel.isViewingOtherSession
                if (viewingOther) {
                    SessionMismatchBanner(
                        sessionName = viewModel.sessionFilter.value ?: "",
                        onSwitchHere = { viewModel.quickSwitchToFilteredSession() },
                        onClear = { viewModel.clearSessionFilter() },
                        modifier = Modifier
                            .align(Alignment.TopCenter)
                            .zIndex(1f)
                    )
                }
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
                    val bannerPad = if (viewingOther) 40.dp else 0.dp
                    LazyColumn(
                        state = listState,
                        modifier = Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(top = 8.dp + bannerPad, bottom = 8.dp)
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
                    enter = fadeIn() + slideInVertically(spring(dampingRatio = Spring.DampingRatioMediumBouncy)) { it },
                    exit = fadeOut() + slideOutVertically { it }
                ) {
                    FilledTonalButton(
                        onClick = {
                            newMessageCount = 0
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
                            if (newMessageCount > 0) "\u2193 $newMessageCount new" else "\u2193 Bottom",
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

        if (showMissionControl) {
            LaunchedEffect(Unit) { viewModel.fetchScheduledTasks() }
            var showAddSchedule by remember { mutableStateOf(false) }
            ModalBottomSheet(
                onDismissRequest = { showMissionControl = false },
                sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
                containerColor = DarkSurface
            ) {
                MissionControlContent(
                    activeTasks = viewModel.activeTasks,
                    scheduledTasks = viewModel.scheduledTasks,
                    onCancel = { viewModel.cancelTask(it) },
                    onPause = { viewModel.pauseTask(it) },
                    onResume = { viewModel.resumeTask(it) },
                    onJumpToSession = { sessionName ->
                        showMissionControl = false
                        viewModel.viewTaskSession(sessionName)
                    },
                    onToggleSchedule = { id, enabled -> viewModel.toggleScheduledTask(id, enabled) },
                    onTriggerSchedule = { viewModel.triggerScheduledTask(it); showMissionControl = false },
                    onEditSchedule = { taskId, prompt, cronExpr, runAt ->
                        viewModel.updateScheduledTask(taskId, prompt, cronExpr, runAt)
                    },
                    onDeleteSchedule = { viewModel.deleteScheduledTask(it) },
                    onAddSchedule = { showAddSchedule = true },
                )
            }
            if (showAddSchedule) {
                AddScheduleDialog(
                    sessions = viewModel.sessionList.map { it.name },
                    onDismiss = { showAddSchedule = false },
                    onCreate = { sessionName, prompt, scheduleType, cronExpr, runAt ->
                        viewModel.createScheduledTask(sessionName, prompt, scheduleType, cronExpr, runAt)
                        showAddSchedule = false
                    }
                )
            }
        }
    }
}

// ==================== Session Mismatch Banner ====================

@Composable
private fun SessionMismatchBanner(
    sessionName: String,
    onSwitchHere: () -> Unit,
    onClear: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Surface(
        color = DarkSurfaceVariant,
        modifier = modifier.fillMaxWidth()
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                "\uD83D\uDC41 Viewing $sessionName",
                color = Color.White,
                fontSize = 12.sp,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.weight(1f),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            TextButton(
                onClick = onSwitchHere,
                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp)
            ) {
                Text("Switch here", color = AccentOrange, fontSize = 11.sp)
            }
            TextButton(
                onClick = onClear,
                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp)
            ) {
                Text("Clear", color = Color.Gray, fontSize = 11.sp)
            }
        }
    }
}

// ==================== Mission Control ====================

private val PHASE_MAP = mapOf(
    "omni" to listOf("architecting", "reviewing", "executing", "auditing"),
    "justdoit" to listOf("implementing", "reviewing", "testing"),
    "deepreview" to listOf("claude_self_review", "codex_reviews_claude", "codex_self_review", "claude_reviews_codex"),
)

private val PHASE_LABELS = mapOf(
    "architecting" to "Architect",
    "executing" to "Execute",
    "auditing" to "Audit",
    "implementing" to "Implement",
    "reviewing" to "Review",
    "testing" to "Test",
    "starting" to "Starting",
    "claude_self_review" to "Claude",
    "codex_reviews_claude" to "Codex Review",
    "codex_self_review" to "Codex Fix",
    "claude_reviews_codex" to "Claude Verify",
)

private fun formatElapsed(seconds: Long): String {
    if (seconds < 0) return "0s"
    val h = seconds / 3600
    val m = (seconds % 3600) / 60
    val s = seconds % 60
    return when {
        h > 0 -> "${h}h ${m}m"
        m > 0 -> "${m}m ${s}s"
        else -> "${s}s"
    }
}

@Composable
private fun MissionControlContent(
    activeTasks: Map<String, ChatViewModel.ActiveTask>,
    scheduledTasks: List<ChatViewModel.ScheduledTask>,
    onCancel: (String) -> Unit,
    onPause: (String) -> Unit,
    onResume: (String) -> Unit,
    onJumpToSession: (String) -> Unit,
    onToggleSchedule: (String, Boolean) -> Unit,
    onTriggerSchedule: (String) -> Unit,
    onEditSchedule: (taskId: String, prompt: String?, cronExpr: String?, runAt: String?) -> Unit,
    onDeleteSchedule: (String) -> Unit,
    onAddSchedule: () -> Unit,
) {
    var selectedTab by remember { mutableIntStateOf(0) }
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(
            "Mission Control",
            style = MaterialTheme.typography.titleMedium,
            color = TopBarTitle,
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)
        )

        TabRow(
            selectedTabIndex = selectedTab,
            containerColor = Color.Transparent,
            contentColor = AccentOrange,
            divider = { HorizontalDivider(color = InputBorder) },
        ) {
            Tab(selected = selectedTab == 0, onClick = { selectedTab = 0 }) {
                Row(
                    modifier = Modifier.padding(12.dp),
                    horizontalArrangement = Arrangement.Center,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text("Active", color = if (selectedTab == 0) AccentOrange else SessionLabel)
                    if (activeTasks.isNotEmpty()) {
                        Spacer(Modifier.width(6.dp))
                        Text(
                            "${activeTasks.size}",
                            fontSize = 11.sp,
                            color = DarkSurface,
                            modifier = Modifier
                                .background(AccentOrange, RoundedCornerShape(8.dp))
                                .padding(horizontal = 5.dp, vertical = 1.dp)
                        )
                    }
                }
            }
            Tab(selected = selectedTab == 1, onClick = { selectedTab = 1 }) {
                Row(
                    modifier = Modifier.padding(12.dp),
                    horizontalArrangement = Arrangement.Center,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text("Scheduled", color = if (selectedTab == 1) AccentOrange else SessionLabel)
                    if (scheduledTasks.isNotEmpty()) {
                        Spacer(Modifier.width(6.dp))
                        Text(
                            "${scheduledTasks.size}",
                            fontSize = 11.sp,
                            color = DarkSurface,
                            modifier = Modifier
                                .background(AccentOrange, RoundedCornerShape(8.dp))
                                .padding(horizontal = 5.dp, vertical = 1.dp)
                        )
                    }
                }
            }
        }

        when (selectedTab) {
            0 -> {
                if (activeTasks.isEmpty()) {
                    Text(
                        "No active tasks.",
                        color = SessionLabel,
                        fontSize = 14.sp,
                        modifier = Modifier.padding(16.dp)
                    )
                } else {
                    LazyColumn(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(max = 500.dp)
                    ) {
                        items(activeTasks.values.toList(), key = { it.session }) { task ->
                            MissionControlRow(
                                task = task,
                                onCancel = { onCancel(task.session) },
                                onPause = { onPause(task.session) },
                                onResume = { onResume(task.session) },
                                onTap = { onJumpToSession(task.session) }
                            )
                            HorizontalDivider(color = InputBorder)
                        }
                    }
                }
            }
            1 -> {
                ScheduledTasksTab(
                    tasks = scheduledTasks,
                    onToggle = onToggleSchedule,
                    onTrigger = onTriggerSchedule,
                    onEdit = onEditSchedule,
                    onDelete = onDeleteSchedule,
                    onAdd = onAddSchedule,
                )
            }
        }

        Spacer(Modifier.height(WindowInsets.navigationBars.asPaddingValues().calculateBottomPadding()))
    }
}

@Composable
private fun MissionControlRow(
    task: ChatViewModel.ActiveTask,
    onCancel: () -> Unit,
    onPause: () -> Unit,
    onResume: () -> Unit,
    onTap: () -> Unit,
) {
    // Ticking elapsed time (freezes when paused)
    var elapsed by remember { mutableLongStateOf(0L) }
    LaunchedEffect(task.started, task.paused) {
        if (task.started > 0 && !task.paused) {
            while (true) {
                elapsed = (System.currentTimeMillis() / 1000) - task.started
                delay(1000)
            }
        }
    }

    val dimAlpha = if (task.paused) 0.5f else 1f

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onTap() }
            .padding(horizontal = 16.dp, vertical = 12.dp)
    ) {
        // Row 1: Session name + mode badge + paused badge + elapsed
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth()
        ) {
            Text(
                task.session,
                color = AccentOrange.copy(alpha = dimAlpha),
                fontSize = 14.sp,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.weight(1f),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            if (task.paused) {
                Surface(
                    shape = RoundedCornerShape(4.dp),
                    color = Color(0xFFFFD54F).copy(alpha = 0.2f)
                ) {
                    Text(
                        "PAUSED",
                        color = Color(0xFFFFD54F),
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                    )
                }
                Spacer(Modifier.width(4.dp))
            }
            Surface(
                shape = RoundedCornerShape(4.dp),
                color = AccentOrange.copy(alpha = 0.15f * dimAlpha)
            ) {
                Text(
                    task.mode.uppercase(),
                    color = AccentOrange.copy(alpha = dimAlpha),
                    fontSize = 10.sp,
                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                )
            }
            Spacer(Modifier.width(8.dp))
            Text(formatElapsed(elapsed), color = SessionLabel.copy(alpha = dimAlpha), fontSize = 11.sp)
        }

        // Row 2: Task description
        if (task.task.isNotEmpty()) {
            Spacer(Modifier.height(4.dp))
            Text(
                task.task,
                color = BotText.copy(alpha = dimAlpha),
                fontSize = 12.sp,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
        }

        // Row 3: Phase stepper
        val phases = PHASE_MAP[task.mode] ?: emptyList()
        if (phases.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            PhaseStepper(phases = phases, currentPhase = task.phase, dimmed = task.paused)
        }

        // Row 4: Step count + Pause/Resume + Stop buttons
        val isAutonomous = task.mode in setOf("omni", "justdoit", "deepreview")
        Spacer(Modifier.height(4.dp))
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth()
        ) {
            Text(
                if (!isAutonomous) "Running"
                else if (task.paused) "Step ${task.step} (paused)"
                else "Step ${task.step}",
                color = SessionLabel,
                fontSize = 11.sp,
                modifier = Modifier.weight(1f)
            )
            if (isAutonomous) {
                if (task.paused) {
                    TextButton(
                        onClick = onResume,
                        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)
                    ) {
                        Text("Resume", color = Color(0xFF66BB6A), fontSize = 12.sp)
                    }
                } else {
                    TextButton(
                        onClick = onPause,
                        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)
                    ) {
                        Text("Pause", color = Color(0xFFFFD54F), fontSize = 12.sp)
                    }
                }
            }
            TextButton(
                onClick = onCancel,
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)
            ) {
                Text("Stop", color = DisconnectedRed, fontSize = 12.sp)
            }
        }
    }
}

@Composable
private fun PhaseStepper(phases: List<String>, currentPhase: String, dimmed: Boolean = false) {
    val currentIndex = phases.indexOf(currentPhase)
    val dimAlpha = if (dimmed) 0.4f else 1f
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth()
    ) {
        phases.forEachIndexed { index, phase ->
            val isComplete = currentIndex >= 0 && index < currentIndex
            val isCurrent = index == currentIndex
            val color = when {
                isComplete -> ConnectedGreen.copy(alpha = dimAlpha)
                isCurrent -> AccentOrange.copy(alpha = dimAlpha)
                else -> SessionLabel.copy(alpha = 0.4f * dimAlpha)
            }
            Box(
                modifier = Modifier
                    .size(if (isCurrent) 10.dp else 8.dp)
                    .clip(CircleShape)
                    .background(color)
            )
            Text(
                PHASE_LABELS[phase] ?: phase,
                fontSize = 9.sp,
                color = color,
                modifier = Modifier.padding(start = 3.dp)
            )
            if (index < phases.size - 1) {
                Box(
                    modifier = Modifier
                        .weight(1f)
                        .height(1.dp)
                        .padding(horizontal = 4.dp)
                        .background(if (isComplete) ConnectedGreen.copy(alpha = 0.5f) else InputBorder)
                )
            }
        }
    }
}

/** Compact session filter button with dropdown menu. */
@Composable
internal fun SessionFilterButton(
    expanded: Boolean,
    onToggle: () -> Unit,
    onDismiss: () -> Unit,
    sessions: List<String>,
    activeFilter: String?,
    onSelect: (String?) -> Unit,
) {
    Box(modifier = Modifier.testTag("session_filter")) {
        IconButton(onClick = onToggle) {
            Text(
                if (activeFilter != null) "\u25C9" else "\u25CE",
                fontSize = 18.sp,
                color = if (activeFilter != null) AccentOrange else SessionLabel
            )
        }
        MaterialTheme(colorScheme = MaterialTheme.colorScheme.copy(surface = DarkSurface)) {
        DropdownMenu(
            expanded = expanded,
            onDismissRequest = onDismiss,
        ) {
            DropdownMenuItem(
                text = { Text("All sessions", fontSize = 13.sp, color = if (activeFilter == null) AccentOrange else BotText) },
                onClick = { onSelect(null) },
                leadingIcon = {
                    if (activeFilter == null) Text("\u2713", color = AccentOrange, fontSize = 14.sp)
                }
            )
            sessions.forEach { s ->
                DropdownMenuItem(
                    text = { Text(s, fontSize = 13.sp, maxLines = 1, color = if (activeFilter == s) AccentOrange else BotText) },
                    onClick = { onSelect(s) },
                    leadingIcon = {
                        if (activeFilter == s) Text("\u2713", color = AccentOrange, fontSize = 14.sp)
                    }
                )
            }
        }
        }
    }
}

/** Animated typing indicator — three bouncy dots in a bot-style bubble.
 *  Uses a manual coroutine loop instead of rememberInfiniteTransition
 *  to avoid continuous recomposition that prevents screen timeout. */
@Composable
private fun TypingIndicator() {
    var activeDot by remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(300)
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
                val isActive = i == activeDot
                val offsetY by animateDpAsState(
                    targetValue = if (isActive) (-3).dp else 0.dp,
                    animationSpec = spring(
                        dampingRatio = Spring.DampingRatioMediumBouncy,
                        stiffness = Spring.StiffnessLow
                    ),
                    label = "dotBounce"
                )
                val alpha by animateFloatAsState(
                    targetValue = if (isActive) 1f else 0.3f,
                    animationSpec = tween(150),
                    label = "dotAlpha"
                )
                Box(
                    modifier = Modifier
                        .offset(y = offsetY)
                        .size(7.dp)
                        .clip(CircleShape)
                        .background(AccentOrange.copy(alpha = alpha))
                )
            }
        }
    }
}

// ==================== Scheduled Tasks ====================

@Composable
private fun ScheduledTasksTab(
    tasks: List<ChatViewModel.ScheduledTask>,
    onToggle: (String, Boolean) -> Unit,
    onTrigger: (String) -> Unit,
    onEdit: (taskId: String, prompt: String?, cronExpr: String?, runAt: String?) -> Unit,
    onDelete: (String) -> Unit,
    onAdd: () -> Unit,
) {
    var editingTask by remember { mutableStateOf<ChatViewModel.ScheduledTask?>(null) }

    Column(modifier = Modifier.fillMaxWidth()) {
        if (tasks.isEmpty()) {
            Text(
                "No scheduled tasks.",
                color = SessionLabel,
                fontSize = 14.sp,
                modifier = Modifier.padding(16.dp)
            )
        } else {
            LazyColumn(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 450.dp)
            ) {
                items(tasks, key = { it.id }) { task ->
                    ScheduledTaskRow(task, onToggle, onTrigger, onDelete, onTap = { editingTask = task })
                    HorizontalDivider(color = InputBorder)
                }
            }
        }

    editingTask?.let { task ->
        EditScheduleDialog(
            task = task,
            onDismiss = { editingTask = null },
            onSave = { prompt, cronExpr, runAt ->
                onEdit(task.id, prompt, cronExpr, runAt)
                editingTask = null
            },
        )
    }
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.End,
        ) {
            OutlinedButton(
                onClick = onAdd,
                border = BorderStroke(1.dp, AccentOrange),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = AccentOrange),
            ) {
                Text("+ Add Schedule")
            }
        }
    }
}

@Composable
private fun ScheduledTaskRow(
    task: ChatViewModel.ScheduledTask,
    onToggle: (String, Boolean) -> Unit,
    onTrigger: (String) -> Unit,
    onDelete: (String) -> Unit,
    onTap: () -> Unit = {},
) {
    val dimAlpha = if (task.enabled) 1f else 0.5f
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onTap() }
            .padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            // Header: cwd basename + Run Now chip
            Row(verticalAlignment = Alignment.CenterVertically) {
                val cwdLabel = task.cwd.trimEnd('/').substringAfterLast('/').ifEmpty { task.cwd }
                Text(
                    cwdLabel,
                    color = AccentOrange.copy(alpha = dimAlpha),
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    "RUN",
                    fontSize = 9.sp,
                    color = DarkSurface,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier
                        .clip(RoundedCornerShape(4.dp))
                        .background(ConnectedGreen)
                        .clickable { onTrigger(task.id) }
                        .padding(horizontal = 6.dp, vertical = 2.dp)
                )
            }

            Spacer(Modifier.height(3.dp))

            // Details
            Column(modifier = Modifier.alpha(dimAlpha)) {
                Text(
                    formatScheduleDescription(task),
                    color = SessionLabel,
                    fontSize = 12.sp,
                )
                Spacer(Modifier.height(2.dp))
                Text(
                    task.prompt,
                    color = BotText.copy(alpha = 0.7f),
                    fontSize = 12.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                if (task.nextRun != null) {
                    Spacer(Modifier.height(2.dp))
                    val relTime = formatRelativeTime(task.nextRun)
                    Text(
                        "Next: $relTime${if (task.runCount > 0) " · ${task.runCount} runs" else ""}",
                        color = SessionLabel,
                        fontSize = 11.sp,
                    )
                }
            }
        }

        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Switch(
                checked = task.enabled,
                onCheckedChange = { onToggle(task.id, it) },
                modifier = Modifier.height(28.dp),
                colors = SwitchDefaults.colors(
                    checkedTrackColor = AccentOrange,
                    uncheckedTrackColor = InputBorder,
                ),
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "Delete",
                color = Color(0xFFCF6679),
                fontSize = 11.sp,
                modifier = Modifier.clickable { onDelete(task.id) },
            )
        }
    }
}

private fun formatScheduleDescription(task: ChatViewModel.ScheduledTask): String {
    if (task.scheduleType == "once") {
        return "Once: ${task.runAt ?: "?"}"
    }
    val expr = task.cronExpr ?: return "?"
    // Try to give human-readable for common patterns
    return when {
        expr == "0 * * * *" -> "Every hour"
        expr == "0 0 * * *" -> "Daily at midnight"
        expr == "0 0 * * 0" -> "Weekly on Sunday"
        expr == "0 0 1 * *" -> "Monthly on the 1st"
        expr.matches(Regex("""(\d+) (\d+) \* \* \*""")) -> {
            val (m, h) = expr.split(" ").take(2)
            "Daily at ${h.padStart(2, '0')}:${m.padStart(2, '0')}"
        }
        expr.matches(Regex("""(\d+) (\d+) \* \* \w+""")) -> {
            val parts = expr.split(" ")
            val m = parts[0]; val h = parts[1]; val dow = parts[4]
            "Weekly ${dow.replaceFirstChar { it.uppercase() }} at ${h.padStart(2, '0')}:${m.padStart(2, '0')}"
        }
        else -> "cron: $expr"
    }
}

private fun formatRelativeTime(epochSecs: Long): String {
    val now = System.currentTimeMillis() / 1000
    val diff = epochSecs - now
    if (diff <= 0) return "now"
    val minutes = diff / 60
    val hours = minutes / 60
    val days = hours / 24
    return when {
        days > 0 -> "${days}d ${hours % 24}h"
        hours > 0 -> "${hours}h ${minutes % 60}m"
        else -> "${minutes}m"
    }
}

private fun Modifier.alpha(a: Float): Modifier = this.graphicsLayer(alpha = a)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddScheduleDialog(
    sessions: List<String>,
    onDismiss: () -> Unit,
    onCreate: (sessionName: String, prompt: String, scheduleType: String, cronExpr: String?, runAt: String?) -> Unit,
) {
    var selectedSession by remember { mutableStateOf(sessions.firstOrNull() ?: "") }
    var prompt by remember { mutableStateOf("") }
    var scheduleType by remember { mutableStateOf("cron") } // "cron" | "once"
    var cronExpr by remember { mutableStateOf("0 9 * * *") }
    var runAtDate by remember { mutableStateOf("") }
    var runAtTime by remember { mutableStateOf("09:00") }
    var sessionExpanded by remember { mutableStateOf(false) }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = DarkSurface,
        titleContentColor = TopBarTitle,
        textContentColor = BotText,
        title = { Text("Add Scheduled Task") },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                // Session picker (used to resolve working directory)
                Text("Working directory (from session)", color = SessionLabel, fontSize = 12.sp)
                ExposedDropdownMenuBox(
                    expanded = sessionExpanded,
                    onExpandedChange = { sessionExpanded = it },
                ) {
                    OutlinedTextField(
                        value = selectedSession,
                        onValueChange = {},
                        readOnly = true,
                        modifier = Modifier.fillMaxWidth().menuAnchor(),
                        trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = sessionExpanded) },
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = AccentOrange,
                            unfocusedBorderColor = InputBorder,
                            focusedTextColor = BotText,
                            unfocusedTextColor = BotText,
                        ),
                        singleLine = true,
                    )
                    ExposedDropdownMenu(
                        expanded = sessionExpanded,
                        onDismissRequest = { sessionExpanded = false },
                        modifier = Modifier.background(DarkSurface),
                    ) {
                        sessions.forEach { name ->
                            DropdownMenuItem(
                                text = { Text(name, color = BotText) },
                                onClick = { selectedSession = name; sessionExpanded = false },
                            )
                        }
                    }
                }

                // Prompt
                Text("Task prompt", color = SessionLabel, fontSize = 12.sp)
                OutlinedTextField(
                    value = prompt,
                    onValueChange = { prompt = it },
                    modifier = Modifier.fillMaxWidth(),
                    minLines = 2,
                    maxLines = 4,
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = AccentOrange,
                        unfocusedBorderColor = InputBorder,
                        focusedTextColor = BotText,
                        unfocusedTextColor = BotText,
                    ),
                    placeholder = { Text("Run tests and fix failures", color = SessionLabel) },
                )

                // Schedule type toggle
                Text("Schedule", color = SessionLabel, fontSize = 12.sp)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilterChip(
                        selected = scheduleType == "cron",
                        onClick = { scheduleType = "cron" },
                        label = { Text("Recurring") },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = AccentOrange,
                            selectedLabelColor = DarkSurface,
                            labelColor = SessionLabel,
                        ),
                    )
                    FilterChip(
                        selected = scheduleType == "once",
                        onClick = { scheduleType = "once" },
                        label = { Text("One-time") },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = AccentOrange,
                            selectedLabelColor = DarkSurface,
                            labelColor = SessionLabel,
                        ),
                    )
                }

                if (scheduleType == "cron") {
                    // Preset buttons
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        listOf("Daily 9am" to "0 9 * * *", "Hourly" to "0 * * * *", "Weekly Mon" to "0 9 * * mon").forEach { (label, expr) ->
                            AssistChip(
                                onClick = { cronExpr = expr },
                                label = { Text(label, fontSize = 11.sp) },
                                border = if (cronExpr == expr) BorderStroke(1.dp, AccentOrange) else AssistChipDefaults.assistChipBorder(true),
                                colors = AssistChipDefaults.assistChipColors(labelColor = BotText),
                            )
                        }
                    }
                    OutlinedTextField(
                        value = cronExpr,
                        onValueChange = { cronExpr = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("Cron expression", color = SessionLabel) },
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = AccentOrange,
                            unfocusedBorderColor = InputBorder,
                            focusedTextColor = BotText,
                            unfocusedTextColor = BotText,
                        ),
                        singleLine = true,
                        placeholder = { Text("0 9 * * *", color = SessionLabel) },
                    )
                } else {
                    OutlinedTextField(
                        value = runAtDate,
                        onValueChange = { runAtDate = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("Date (YYYY-MM-DD)", color = SessionLabel) },
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = AccentOrange,
                            unfocusedBorderColor = InputBorder,
                            focusedTextColor = BotText,
                            unfocusedTextColor = BotText,
                        ),
                        singleLine = true,
                        placeholder = { Text("2026-03-15", color = SessionLabel) },
                    )
                    OutlinedTextField(
                        value = runAtTime,
                        onValueChange = { runAtTime = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("Time (HH:MM)", color = SessionLabel) },
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = AccentOrange,
                            unfocusedBorderColor = InputBorder,
                            focusedTextColor = BotText,
                            unfocusedTextColor = BotText,
                        ),
                        singleLine = true,
                        placeholder = { Text("14:00", color = SessionLabel) },
                    )
                }

            }
        },
        confirmButton = {
            val canCreate = selectedSession.isNotEmpty() && prompt.isNotEmpty()
            TextButton(
                onClick = {
                    if (canCreate) {
                        val finalCronExpr = if (scheduleType == "cron") cronExpr else null
                        val finalRunAt = if (scheduleType == "once") "${runAtDate}T${runAtTime}" else null
                        onCreate(selectedSession, prompt, scheduleType, finalCronExpr, finalRunAt)
                    }
                },
                enabled = canCreate,
            ) {
                Text("Create", color = if (canCreate) AccentOrange else SessionLabel)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel", color = SessionLabel)
            }
        },
    )
}

@Composable
private fun EditScheduleDialog(
    task: ChatViewModel.ScheduledTask,
    onDismiss: () -> Unit,
    onSave: (prompt: String?, cronExpr: String?, runAt: String?) -> Unit,
) {
    var prompt by remember { mutableStateOf(task.prompt) }
    var cronExpr by remember { mutableStateOf(task.cronExpr ?: "0 9 * * *") }
    var runAtDate by remember { mutableStateOf(task.runAt?.substringBefore("T") ?: "") }
    var runAtTime by remember { mutableStateOf(task.runAt?.substringAfter("T")?.take(5) ?: "09:00") }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = DarkSurface,
        titleContentColor = TopBarTitle,
        textContentColor = BotText,
        title = { Text("Edit: ${task.cwd.trimEnd('/').substringAfterLast('/').ifEmpty { task.cwd }}") },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Text("Task prompt", color = SessionLabel, fontSize = 12.sp)
                OutlinedTextField(
                    value = prompt,
                    onValueChange = { prompt = it },
                    modifier = Modifier.fillMaxWidth(),
                    minLines = 2,
                    maxLines = 4,
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = AccentOrange,
                        unfocusedBorderColor = InputBorder,
                        focusedTextColor = BotText,
                        unfocusedTextColor = BotText,
                    ),
                )

                if (task.scheduleType == "cron") {
                    Text("Cron expression", color = SessionLabel, fontSize = 12.sp)
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        listOf("Daily 9am" to "0 9 * * *", "Hourly" to "0 * * * *", "Weekly Mon" to "0 9 * * mon").forEach { (label, expr) ->
                            AssistChip(
                                onClick = { cronExpr = expr },
                                label = { Text(label, fontSize = 11.sp) },
                                border = if (cronExpr == expr) BorderStroke(1.dp, AccentOrange) else AssistChipDefaults.assistChipBorder(true),
                                colors = AssistChipDefaults.assistChipColors(labelColor = BotText),
                            )
                        }
                    }
                    OutlinedTextField(
                        value = cronExpr,
                        onValueChange = { cronExpr = it },
                        modifier = Modifier.fillMaxWidth(),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = AccentOrange,
                            unfocusedBorderColor = InputBorder,
                            focusedTextColor = BotText,
                            unfocusedTextColor = BotText,
                        ),
                        singleLine = true,
                    )
                } else {
                    Text("Run at", color = SessionLabel, fontSize = 12.sp)
                    OutlinedTextField(
                        value = runAtDate,
                        onValueChange = { runAtDate = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("Date (YYYY-MM-DD)", color = SessionLabel) },
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = AccentOrange,
                            unfocusedBorderColor = InputBorder,
                            focusedTextColor = BotText,
                            unfocusedTextColor = BotText,
                        ),
                        singleLine = true,
                    )
                    OutlinedTextField(
                        value = runAtTime,
                        onValueChange = { runAtTime = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("Time (HH:MM)", color = SessionLabel) },
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = AccentOrange,
                            unfocusedBorderColor = InputBorder,
                            focusedTextColor = BotText,
                            unfocusedTextColor = BotText,
                        ),
                        singleLine = true,
                    )
                }
            }
        },
        confirmButton = {
            TextButton(
                onClick = {
                    val newPrompt = if (prompt != task.prompt) prompt else null
                    val newCron = if (task.scheduleType == "cron" && cronExpr != task.cronExpr) cronExpr else null
                    val newRunAt = if (task.scheduleType == "once") "${runAtDate}T${runAtTime}" else null
                    onSave(newPrompt, newCron, newRunAt)
                },
                enabled = prompt.isNotEmpty(),
            ) {
                Text("Save", color = if (prompt.isNotEmpty()) AccentOrange else SessionLabel)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel", color = SessionLabel)
            }
        },
    )
}
