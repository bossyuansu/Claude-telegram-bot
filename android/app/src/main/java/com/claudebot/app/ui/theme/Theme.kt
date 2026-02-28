package com.claudebot.app.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val DarkColors = darkColorScheme(
    primary = AccentOrange,
    onPrimary = Color.White,
    primaryContainer = AccentOrange,
    onPrimaryContainer = Color.White,
    secondary = AccentOrangeLight,
    background = DarkBg,
    onBackground = BotText,
    surface = DarkSurface,
    onSurface = BotText,
    surfaceVariant = DarkSurfaceVariant,
    onSurfaceVariant = SessionLabel,
    outline = InputBorder,
)

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DarkColors,
        content = content
    )
}
