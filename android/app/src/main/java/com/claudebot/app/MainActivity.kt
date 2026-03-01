package com.claudebot.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.lifecycle.viewmodel.compose.viewModel
import com.claudebot.app.ui.screens.ChatScreen
import com.claudebot.app.ui.screens.SettingsScreen
import com.claudebot.app.ui.theme.AppTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            AppTheme {
                val vm: ChatViewModel = viewModel()
                val showSettings by vm.showSettings

                if (showSettings) {
                    SettingsScreen(vm)
                } else {
                    ChatScreen(vm)
                }
            }
        }
    }
}
