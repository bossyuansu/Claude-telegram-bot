package com.claudebot.app.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.rememberTransformableState
import androidx.compose.foundation.gestures.transformable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import coil.compose.AsyncImage
import coil.request.ImageRequest
import com.claudebot.app.ui.theme.AccentOrange

private enum class ImageStatus { Loading, Loaded, Failed }

@Composable
internal fun InlineImage(
    url: String,
    altText: String,
    onOpenUrl: (String) -> Unit,
    captionColor: Color,
    linkColor: Color,
) {
    val context = LocalContext.current
    val request = remember(context, url) { ImageRequest.Builder(context).data(url).build() }
    var status by remember(url) { mutableStateOf(ImageStatus.Loading) }
    var expanded by remember(url) { mutableStateOf(false) }
    Column(Modifier.fillMaxWidth().padding(vertical = 6.dp)) {
        Box(
            Modifier.fillMaxWidth().height(240.dp)
                .clip(RoundedCornerShape(8.dp)).background(Color.White)
                .clickable(enabled = status == ImageStatus.Loaded) { expanded = true }
                .testTag(when (status) {
                    ImageStatus.Loading -> "inline-image-loading"
                    ImageStatus.Loaded -> "inline-image-loaded"
                    ImageStatus.Failed -> "inline-image-error"
                })
                .padding(8.dp),
            contentAlignment = Alignment.Center,
        ) {
            AsyncImage(
                model = request,
                contentDescription = altText.ifBlank { "Image" },
                modifier = Modifier.fillMaxSize(),
                contentScale = ContentScale.Fit,
                onLoading = { status = ImageStatus.Loading },
                onSuccess = { status = ImageStatus.Loaded },
                onError = { status = ImageStatus.Failed },
            )
            when (status) {
                ImageStatus.Loading -> CircularProgressIndicator(Modifier.size(28.dp), color = AccentOrange)
                ImageStatus.Failed -> Text("Couldn't load image", color = Color.DarkGray, fontSize = 13.sp)
                ImageStatus.Loaded -> Unit
            }
        }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(
                altText.ifBlank { "Image" }, color = captionColor, fontSize = 11.sp,
                maxLines = 2, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f),
            )
            TextButton(onClick = { onOpenUrl(url) }) { Text("Open original", color = linkColor, fontSize = 11.sp) }
        }
    }
    if (expanded) ImageViewer(url, altText, onOpenUrl) { expanded = false }
}

@Composable
private fun ImageViewer(url: String, altText: String, onOpenUrl: (String) -> Unit, onDismiss: () -> Unit) {
    var status by remember(url) { mutableStateOf(ImageStatus.Loading) }
    var scale by remember(url) { mutableFloatStateOf(1f) }
    var offset by remember(url) { mutableStateOf(Offset.Zero) }
    val transform = rememberTransformableState { zoom, pan, _ ->
        scale = (scale * zoom).coerceIn(1f, 5f)
        offset = if (scale == 1f) Offset.Zero else offset + pan
    }
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Column(Modifier.fillMaxSize().background(Color.Black).systemBarsPadding().testTag("image-viewer")) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                TextButton(onClick = onDismiss) { Text("Close", color = Color.White) }
                if (scale > 1f) {
                    TextButton(onClick = { scale = 1f; offset = Offset.Zero }) { Text("Reset zoom", color = Color.White) }
                }
                TextButton(onClick = { onOpenUrl(url) }) { Text("Open original", color = Color.White) }
            }
            Box(
                Modifier.fillMaxWidth().weight(1f).clipToBounds().transformable(transform),
                contentAlignment = Alignment.Center,
            ) {
                AsyncImage(
                    model = url,
                    contentDescription = altText.ifBlank { "Image" },
                    contentScale = ContentScale.Fit,
                    onLoading = { status = ImageStatus.Loading },
                    onSuccess = { status = ImageStatus.Loaded },
                    onError = { status = ImageStatus.Failed },
                    modifier = Modifier.fillMaxSize().graphicsLayer {
                        scaleX = scale; scaleY = scale
                        translationX = offset.x; translationY = offset.y
                    }.background(Color.White).padding(8.dp),
                )
                when (status) {
                    ImageStatus.Loading -> CircularProgressIndicator(Modifier.size(28.dp), color = AccentOrange)
                    ImageStatus.Failed -> Text("Couldn't load image", color = Color.DarkGray)
                    ImageStatus.Loaded -> Unit
                }
            }
        }
    }
}
