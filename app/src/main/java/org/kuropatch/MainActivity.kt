package org.kuropatch

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import android.widget.TextView

class MainActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val textView = TextView(this).apply {
            text = "KuroPatch Game Translator Engine"
            textSize = 20f
            setPadding(32, 32, 32, 32)
        }
        setContentView(textView)
    }
}
