{% extends 'base.tpl' %}
{% block ext %}
<P8400>1</P8400>
{% endblock %}

{% block model_specific_config %}
    {% if XX_mpk -%}
      {% for code, value in XX_mpk -%}
        <{{ code }}>{{ value }}</{{ code }}>
      {% endfor -%}
    {% endif -%}
{% endblock %}