package org.kuropatch

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import android.widget.TextView

/**
 * Launcher activity.
 *
 * The translation engine is Python (see core.py / main.py), so this activity
 * only needs to exist, carry the app label and reference a valid theme.
 */
class MainActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val textView = TextView(this).apply {
            text = getString(R.string.engine_title)
            textSize = 20f
            setPadding(32, 32, 32, 32)
        }
        setContentView(textView)
    }
}
